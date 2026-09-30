"""A testnet funding tap for the demo server: gives a fresh address a little gas and some mock
tokens so a visitor can try Foliant on Fuji without finding a faucet.

    POST /tap  {"address": "0x..."}  ->  {"address", "avaxWei", "tokens", "transactions": [..]}

Rules (TAP-REVIEW.md): one attempt per address, ever, and at most FOLIANT_TAP_PER_HOUR
attempts an hour, both charged *before* anything is broadcast and both persisted, so a failed
or slow transaction cannot be retried for free; only externally-owned recipients (a contract
could reject the value after gas is spent); the mint goes first (it fails at estimateGas,
spending nothing) and the AVAX second; receipts are waited for with a short timeout; and the
tap refuses cleanly (503, nothing charged) when its own wallet cannot cover what it is about
to send. State is one JSON file guarded by a lock file held for the whole attempt, so two
instances over one directory cannot both fund an address. The wallet behind FOLIANT_TAP_KEY is
a testnet key, used by nothing else, and must never hold real funds.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError, TimeExhausted, Web3RPCError

from foliant.chain import ChainLedger

RECEIPT_TIMEOUT = 60  # seconds; Fuji blocks are ~2 s, so this is a stuck transaction, not a slow one
GAS_HEADROOM = 1.25   # over the node's gas price, so a fee rise between quote and inclusion does not strand us


class TapError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class NotBroadcast(Exception):
    """A transaction the node rejected before it entered the mempool: nothing is in flight."""


class Tap:
    def __init__(self, ledger: ChainLedger, key: str, token: str, *, avax_wei: int, tokens: int,
                 per_hour: int, state_dir: Optional[str] = None):
        self.L = ledger
        self.key = Account.from_key(key)
        self.token = self.L.token(token)
        self.avax_wei = avax_wei
        self.tokens = tokens
        self.per_hour = per_hour
        self._lock = threading.Lock()
        d = Path(state_dir or os.environ.get("FOLIANT_STATE_DIR", "."))
        d.mkdir(parents=True, exist_ok=True)
        self._path = d / f"foliant-tap-{self.L.chain_id}-{self.key.address.lower()}.json"
        self._given: dict[str, str] = {}   # address -> "funded" | "failed:<reason>"
        self._recent: list[float] = []     # attempt times in the last hour
        self._balance_cache: tuple[float, int] = (0.0, 0)
        self._given_count = 0  # for status(): reported without taking the locks (TAP-REVIEW R2-3)
        with self._file_lock():
            self._load()
            self._given_count = len(self._given)

    @property
    def address(self) -> str:
        return self.key.address

    def balance(self) -> int:
        return self.L.w3.eth.get_balance(self.key.address)

    def _cached_balance(self, max_age: float = 30) -> int:
        t, b = self._balance_cache
        if time.time() - t > max_age:
            b = self.balance()
            self._balance_cache = (time.time(), b)
        return b

    def status(self) -> dict:
        """For GET /chain. Nothing here takes a lock or waits on a give in progress; the balance is
        cached for 30 s and the count is this instance's last known figure."""
        return {"tap": self.key.address, "balanceWei": self._cached_balance(), "avaxWei": self.avax_wei,
                "tokens": self.tokens, "perHour": self.per_hour, "given": self._given_count}

    def give(self, address: str) -> dict:
        to = self._recipient(address)
        # one give at a time, and a caller never waits for another's chain round-trips: a request that
        # finds the tap busy is told so at once, rather than parking a server thread (TAP-REVIEW R2-3)
        if not self._lock.acquire(blocking=False):
            raise TapError(503, "the tap is serving someone else; retry in a few seconds")
        try:
            with self._file_lock(blocking=False):
                return self._give_locked(to)
        except TapError:
            raise
        except Exception as e:  # noqa: BLE001  an RPC failure in the pre-checks: nothing was charged
            raise TapError(502, f"chain unavailable: {type(e).__name__}") from None
        finally:
            self._lock.release()

    def _give_locked(self, to: str) -> dict:
        w3 = self.L.w3
        self._load()  # another instance over the same directory may have moved on
        key = to.lower()
        if key in self._given:
            raise TapError(429, "this address has already been funded (or an attempt was made)")
        now = time.time()
        self._recent = [t for t in self._recent if t > now - 3600]
        if len(self._recent) >= self.per_hour:
            raise TapError(429, "the tap is busy; try again in an hour")
        if w3.eth.get_code(to):
            raise TapError(400, "the address is a contract; the tap funds externally-owned addresses only")
        gas_price = int(w3.eth.gas_price * GAS_HEADROOM)
        need = self.avax_wei + gas_price * (21_000 + 80_000)  # the transfer and the mint, with headroom
        if self._cached_balance() < need and self._cached_balance(0) < need:
            raise TapError(503, "the tap is dry; it will be refilled")
        latest = w3.eth.get_transaction_count(self.key.address, "latest")
        if w3.eth.get_transaction_count(self.key.address, "pending") != latest:
            # a transaction of ours is still in flight: a new one would queue behind it and time out,
            # spending the visitor's one attempt on a failure (R2-1)
            raise TapError(503, "the tap has a transaction in flight; retry in a minute")
        # the attempt is charged now: a failure after anything is broadcast does not make the address
        # eligible again (R2-2: a failure *before* any broadcast releases the address, keeps the slot)
        self._given[key] = "pending"
        self._recent.append(now)
        self._save()
        txs: list[str] = []
        broadcast = False  # anything may be in flight
        sent = 0           # confirmed sends
        try:
            nonce = latest
            if self.tokens:
                try:  # estimateGas runs the mint: a revert here has spent nothing
                    tx = self.token.functions.mint(to, self.tokens).build_transaction(
                        {"from": self.key.address, "nonce": nonce, "gasPrice": gas_price, "chainId": self.L.chain_id})
                except (ContractCustomError, ContractLogicError) as e:
                    raise TapError(502, f"mint refused: {e}") from None
                broadcast = True  # from here an ambiguous failure counts as sent; only NotBroadcast says otherwise
                txs.append(self._send(tx))
                sent += 1
                nonce += 1
            if self.avax_wei:
                broadcast = True
                txs.append(self._send({"to": to, "value": self.avax_wei, "gas": 21_000, "gasPrice": gas_price,
                                       "nonce": nonce, "chainId": self.L.chain_id}))
        except NotBroadcast as e:
            # the node refused the transaction (insufficient funds, say): if nothing of ours was broadcast
            # before it the address is released; if the mint already went, the address is spent
            self._balance_cache = (0.0, 0)
            if sent:  # the mint went; this address has had its attempt
                self._given[key] = f"failed:{e}"
            else:
                self._given.pop(key, None)
            self._given_count = len(self._given)
            try:
                self._save()
            except OSError:
                pass
            raise TapError(502, str(e)) from None
        except Exception as e:  # noqa: BLE001
            self._balance_cache = (0.0, 0)  # whatever happened, our balance may have moved (R3-1)
            err = e if isinstance(e, TapError) else TapError(502, f"funding failed: {type(e).__name__}")
            if broadcast:
                self._given[key] = f"failed:{err}"
            else:
                self._given.pop(key, None)  # nothing reached the chain: the address may try again
            self._given_count = len(self._given)
            try:
                self._save()
            except OSError:
                pass  # the original error is the one to report; the in-memory state is still right
            raise err from None
        self._balance_cache = (0.0, 0)
        self._given[key] = "funded"
        self._save()
        self._given_count = len(self._given)
        return {"address": to, "avaxWei": self.avax_wei, "tokens": self.tokens, "transactions": txs}

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _recipient(address) -> str:
        if not isinstance(address, str) or not Web3.is_address(address):
            raise TapError(400, "address must be an EVM address")
        hexpart = address[2:]
        if hexpart != hexpart.lower() and hexpart != hexpart.upper() and not Web3.is_checksum_address(address):
            raise TapError(400, "address has a bad checksum (a typo?); give it in lowercase to skip the check")
        return Web3.to_checksum_address(address)

    def _send(self, tx: dict) -> str:
        w3 = self.L.w3
        try:
            h = w3.eth.send_raw_transaction(self.key.sign_transaction(tx).raw_transaction)
        except Web3RPCError as e:  # the node answered and refused it: nothing is in flight
            raise NotBroadcast(f"transaction rejected by the node: {e}") from None
        # any other failure (a transport error, say) is ambiguous: the node may have accepted it, so the
        # caller treats it as broadcast
        try:
            r = w3.eth.wait_for_transaction_receipt(h, timeout=RECEIPT_TIMEOUT)
        except TimeExhausted:
            raise TapError(504, f"funding transaction not confirmed in {RECEIPT_TIMEOUT}s: 0x{h.hex()}") from None
        if r["status"] != 1:
            raise TapError(502, f"funding transaction reverted: 0x{h.hex()}")
        return "0x" + h.hex()

    class _FileLock:
        """Cross-process exclusive lock on a sibling .lock file (the gate's pattern in foliant/chain.py)."""

        def __init__(self, path: Path, blocking: bool = True):
            self.path = path.with_suffix(".lock")
            self.blocking = blocking
            self.fd = None

        def __enter__(self):
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self.fd)
                self.fd = None
                raise TapError(503, "the tap is serving someone else; retry in a few seconds") from None
            return self

        def __exit__(self, *exc):
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)

    def _file_lock(self, blocking: bool = True) -> "_FileLock":
        return self._FileLock(self._path, blocking)

    def _load(self) -> None:
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text())
            except ValueError:  # a corrupt file: fail closed, recover by hand (the error names the file)
                raise TapError(503, f"tap state file unreadable: {self._path.name}") from None
            self._given = dict(saved.get("given", {}))
            self._recent = [float(t) for t in saved.get("recent", [])]
            self._given_count = len(self._given)

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix=self._path.name, dir=self._path.parent)
        with os.fdopen(fd, "w") as f:
            json.dump({"given": self._given, "recent": self._recent}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)


def tap_from_env(ledger: ChainLedger, env=os.environ) -> Optional[Tap]:
    """Build the tap from FOLIANT_TAP_KEY (and optional FOLIANT_TAP_AVAX in AVAX, FOLIANT_TAP_TOKENS in
    whole tokens, FOLIANT_TAP_PER_HOUR); None when no key is set, in which case there is no tap."""
    key = env.get("FOLIANT_TAP_KEY")
    if not key:
        return None
    decimals = ledger.token(env["FOLIANT_TOKEN"]).functions.decimals().call()
    return Tap(ledger, key, env["FOLIANT_TOKEN"],
               avax_wei=Web3.to_wei(env.get("FOLIANT_TAP_AVAX", "0.02"), "ether"),
               tokens=int(env.get("FOLIANT_TAP_TOKENS", "1000")) * 10 ** decimals,
               per_hour=int(env.get("FOLIANT_TAP_PER_HOUR", "20")))
