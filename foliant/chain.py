"""EVM backend: the Foliant contracts on a live chain, behind the same shapes as the in-memory ledger.

Two halves, matching the reference:

  ChainAgent   the agent side. Holds an EVM key, sends its own transactions (the contracts
               authenticate the agent as msg.sender), and signs EIP-712 channel and pool
               updates off-chain. Same method names as foliant.agent.Agent.
  ChainGate    the provider side. Serves x402 terms, verifies updates against chain state,
               issues receipts, and settles every session in as few transactions as possible.
               Same wire format as foliant.x402.PaymentGate (X-PAYMENT / X-PAYMENT-RESPONSE),
               with the update carried as {account, seq, balance, [epoch], sig}.

ChainLedger is the shared read side: contract handles, views shaped like foliant.node's.

Requires the `chain` extra: pip install "foliant-protocol[chain]".
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import Request
from web3 import Web3

from .errors import FoliantError, PolicyViolation

HDR_PAYMENT = "X-PAYMENT"
HDR_RECEIPT = "X-PAYMENT-RESPONSE"
X402_VERSION = 1

NETWORKS = {43113: "avalanche-fuji", 43114: "avalanche", 31337: "anvil"}


def _abi(name: str) -> dict:
    return json.loads(resources.files("foliant").joinpath(f"abi/{name}.json").read_text())


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, sort_keys=True).encode()).decode()


def _unb64(s: str) -> dict:
    return json.loads(base64.b64decode(s))


def _hex(b: bytes) -> str:
    return "0x" + b.hex()


# --------------------------------------------------------------------------- policy


@dataclass
class ChainPolicy:
    """PolicyInput as the contract takes it. Amounts are in the token's smallest unit."""

    per_tx_max: int
    per_window_max: int
    window_secs: int
    allow_list: Optional[list[str]] = None  # None = any payee
    deny_list: list[str] = field(default_factory=list)
    expiry: int = 0  # unix seconds; 0 = never
    escalation: str = "0x0000000000000000000000000000000000000000"

    def as_tuple(self) -> tuple:
        return (
            self.per_tx_max, self.per_window_max, self.window_secs,
            self.allow_list is not None, [Web3.to_checksum_address(a) for a in (self.allow_list or [])],
            [Web3.to_checksum_address(a) for a in self.deny_list], self.expiry,
            Web3.to_checksum_address(self.escalation),
        )

    @classmethod
    def from_tuple(cls, t: tuple) -> "ChainPolicy":
        return cls(t[0], t[1], t[2], list(t[4]) if t[3] else None, list(t[5]), t[6], t[7])


# --------------------------------------------------------------------------- ledger (read side)


class ChainLedger:
    def __init__(self, rpc_url: str, accounts: str, channels: str, pools: str, w3: Optional[Web3] = None):
        self.w3 = w3 or Web3(Web3.HTTPProvider(rpc_url))
        self.accounts = self.w3.eth.contract(address=Web3.to_checksum_address(accounts), abi=_abi("AgentAccounts")["abi"])
        self.channels = self.w3.eth.contract(address=Web3.to_checksum_address(channels), abi=_abi("PaymentChannels")["abi"])
        self.pools = self.w3.eth.contract(address=Web3.to_checksum_address(pools), abi=_abi("Pools")["abi"])
        self.chain_id = self.w3.eth.chain_id

    @property
    def network(self) -> str:
        return NETWORKS.get(self.chain_id, f"eip155:{self.chain_id}")

    @property
    def now(self) -> int:
        return self.w3.eth.get_block("latest")["timestamp"]

    def token(self, address: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=_abi("MockERC20")["abi"])

    # views shaped like foliant.node's

    def account(self, acct_id: bytes) -> dict:
        a = self.accounts
        if not a.functions.exists(acct_id).call():
            raise FoliantError("no account")
        return {
            "id": _hex(acct_id),
            "owner": a.functions.ownerOf(acct_id).call(),
            "signer": a.functions.signerOf(acct_id).call(),
            "parent": _hex(a.functions.parentOf(acct_id).call()),
            "policy": ChainPolicy.from_tuple(a.functions.policyOf(acct_id).call()),
            "spent_in_window": a.functions.spentInWindow(acct_id).call(),
            "escalation_nonce": a.functions.escalationNonce(acct_id).call(),
        }

    def balance(self, acct_id: bytes, token: str) -> int:
        return self.accounts.functions.balanceOf(acct_id, Web3.to_checksum_address(token)).call()

    def channel(self, cid: bytes) -> dict:
        c = self.channels.functions.get(cid).call()
        return {"id": _hex(cid), "payer": _hex(c[0]), "signer": c[1], "payee": c[2], "token": c[3], "deposit": c[4],
                "paid": c[5], "seq": c[6], "timeout_secs": c[7], "closing_at": c[8] or None, "closed": c[9]}

    def pool(self, pid: bytes) -> dict:
        p = self.pools.functions.get(pid).call()
        return {"id": _hex(pid), "coordinator": p[0], "token": p[1], "timeout_secs": p[2]}

    def claim(self, pid: bytes, acct_id: bytes) -> Optional[dict]:
        c = self.pools.functions.claimOf(pid, acct_id).call()
        if not c[7]:
            return None
        return {"deposit": c[0], "paid": c[1], "seq": c[2], "exit_at": c[3] or None, "epoch": c[4], "signer": c[5],
                "exited": c[6]}

    # EIP-712 digests (for verification and for signing)

    def _domain(self, contract, name: str) -> dict:
        return {"name": name, "version": "1", "chainId": self.chain_id, "verifyingContract": contract.address}

    def channel_update_types(self) -> tuple[dict, dict]:
        return self._domain(self.channels, "FoliantPaymentChannels"), {
            "ChannelUpdate": [{"name": "channel", "type": "bytes32"}, {"name": "account", "type": "bytes32"},
                              {"name": "seq", "type": "uint64"}, {"name": "balance", "type": "uint256"}]}

    def pool_update_types(self) -> tuple[dict, dict]:
        return self._domain(self.pools, "FoliantPools"), {
            "PoolUpdate": [{"name": "pool", "type": "bytes32"}, {"name": "account", "type": "bytes32"},
                           {"name": "epoch", "type": "uint64"}, {"name": "seq", "type": "uint64"},
                           {"name": "balance", "type": "uint256"}]}

    def recover_channel_update(self, u: dict) -> str:
        domain, types = self.channel_update_types()
        msg = {"channel": bytes.fromhex(u["id"][2:]), "account": bytes.fromhex(u["account"][2:]),
               "seq": u["seq"], "balance": u["balance"]}
        return _recover_typed(domain, types, msg, u["sig"])

    def recover_pool_update(self, u: dict) -> str:
        domain, types = self.pool_update_types()
        msg = {"pool": bytes.fromhex(u["id"][2:]), "account": bytes.fromhex(u["account"][2:]),
               "epoch": u["epoch"], "seq": u["seq"], "balance": u["balance"]}
        return _recover_typed(domain, types, msg, u["sig"])


def _recover_typed(domain: dict, types: dict, msg: dict, sig: str) -> str:
    from eth_account.messages import encode_typed_data
    return Account.recover_message(encode_typed_data(domain, types, msg), signature=sig)


# --------------------------------------------------------------------------- agent side


class ChainAgent:
    """An agent operating one account on the contracts with an EVM key (owner and signer, unless attached)."""

    def __init__(self, ledger: ChainLedger, private_key: str, account_id: Optional[bytes] = None):
        self.L = ledger
        self.key = Account.from_key(private_key)
        self.address = self.key.address
        self.account_id: Optional[bytes] = account_id
        self.latest: dict[bytes, dict] = {}  # channel/pool id -> last signed update
        self._seq: dict[bytes, int] = {}

    # transactions

    def _send(self, fn, value: int = 0) -> dict:
        w3 = self.L.w3
        tx = fn.build_transaction({"from": self.address, "nonce": w3.eth.get_transaction_count(self.address),
                                   "value": value, "chainId": self.L.chain_id})
        signed = self.key.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h)
        if r["status"] != 1:
            raise FoliantError(f"transaction reverted: {h.hex()}")
        return r

    def register(self, policy: ChainPolicy, salt: int = 0, signer: Optional[str] = None) -> bytes:
        signer = signer or self.address
        self._send(self.L.accounts.functions.register(signer, policy.as_tuple(), salt))
        self.account_id = self.L.accounts.functions.accountId(self.address, signer, salt).call()
        return self.account_id

    def deposit(self, token: str, amount: int) -> None:
        t = self.L.token(token)
        self._send(t.functions.approve(self.L.accounts.address, amount))
        self._send(self.L.accounts.functions.deposit(self.account_id, t.address, amount))

    def transfer(self, token: str, payee: str, amount: int) -> None:
        self._send(self.L.accounts.functions.transfer(self.account_id, Web3.to_checksum_address(token),
                                                      Web3.to_checksum_address(payee), amount, b"", 0))

    def delegate(self, signer: str, policy: ChainPolicy, token: str, fund: int, salt: int = 0) -> bytes:
        self._send(self.L.accounts.functions.delegate(self.account_id, Web3.to_checksum_address(signer), policy.as_tuple(),
                                                      salt, Web3.to_checksum_address(token), fund))
        return self.L.accounts.functions.childId(self.account_id, Web3.to_checksum_address(signer), salt).call()

    def recall(self, child: bytes, token: str, amount: int = 0) -> None:
        self._send(self.L.accounts.functions.recall(self.account_id, child, Web3.to_checksum_address(token), amount))

    def open_channel(self, payee: str, token: str, deposit: int, timeout_secs: int = 3600, salt: int = 0) -> bytes:
        payee = Web3.to_checksum_address(payee)
        self._send(self.L.channels.functions.open(self.account_id, payee, Web3.to_checksum_address(token), deposit,
                                                  timeout_secs, salt, b"", 0))
        return self.L.channels.functions.channelId(self.account_id, payee, salt).call()

    def join_pool(self, pid: bytes, deposit: int) -> None:
        self._send(self.L.pools.functions.join(pid, self.account_id, deposit, b"", 0))
        self.latest.pop(pid, None)
        self._seq.pop(pid, None)

    def close_channel(self, cid: bytes) -> None:
        u = self.latest.get(cid)
        args = (cid, u["seq"], u["balance"], bytes.fromhex(u["sig"][2:])) if u else (cid, 0, 0, b"")
        self._send(self.L.channels.functions.beginClose(*args))

    def finalize_close(self, cid: bytes) -> None:
        self._send(self.L.channels.functions.finalizeClose(cid))

    def begin_exit(self, pid: bytes) -> None:
        u = self.latest.get(pid)
        args = (pid, self.account_id, u["seq"], u["balance"], bytes.fromhex(u["sig"][2:])) if u \
            else (pid, self.account_id, 0, 0, b"")
        self._send(self.L.pools.functions.beginExit(*args))

    def finalize_exit(self, pid: bytes) -> None:
        self._send(self.L.pools.functions.finalizeExit(pid, self.account_id))
        self.latest.pop(pid, None)
        self._seq.pop(pid, None)

    # off-chain updates (EIP-712). Nothing is sent.

    def pay_channel(self, cid: bytes, amount: int) -> dict:
        ch = self.L.channel(cid)
        prev = self.latest[cid]["balance"] if cid in self.latest else ch["paid"]
        balance = prev + amount
        if balance > ch["deposit"]:
            raise PolicyViolation("channel deposit exhausted")
        seq = self._seq.get(cid, ch["seq"]) + 1
        domain, types = self.L.channel_update_types()
        sig = self.key.sign_typed_data(domain, types, {"channel": cid, "account": self.account_id, "seq": seq,
                                                       "balance": balance}).signature
        u = {"kind": "channel", "id": _hex(cid), "account": _hex(self.account_id), "seq": seq, "balance": balance,
             "sig": _hex(sig)}
        self._seq[cid], self.latest[cid] = seq, u
        return u

    def pay_pool(self, pid: bytes, amount: int) -> dict:
        claim = self.L.claim(pid, self.account_id)
        if claim is None or claim["exited"]:
            raise FoliantError("not a member of this pool")
        prev = self.latest[pid]["balance"] if pid in self.latest else claim["paid"]
        balance = prev + amount
        if balance > claim["deposit"]:
            raise PolicyViolation("pool deposit exhausted")
        seq = self._seq.get(pid, claim["seq"]) + 1
        domain, types = self.L.pool_update_types()
        sig = self.key.sign_typed_data(domain, types, {"pool": pid, "account": self.account_id, "epoch": claim["epoch"],
                                                       "seq": seq, "balance": balance}).signature
        u = {"kind": "pool", "id": _hex(pid), "account": _hex(self.account_id), "epoch": claim["epoch"], "seq": seq,
             "balance": balance, "sig": _hex(sig)}
        self._seq[pid], self.latest[pid] = seq, u
        return u


# --------------------------------------------------------------------------- provider side


@dataclass
class ChainOffer:
    provider: str          # payee address (receives channel settlements; coordinator of the pool)
    token: str
    price_per_unit: int
    unit: str = "call"
    pool_id: Optional[bytes] = None
    descriptor: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return Web3.keccak(text=json.dumps({"p": self.provider, "t": self.token, "u": self.price_per_unit,
                                            "pool": _hex(self.pool_id) if self.pool_id else None}, sort_keys=True)).hex()


@dataclass
class ChainPayment:
    scheme: str
    obj_id: bytes
    account: bytes
    update: dict
    paid: int


class PaymentRequired(Exception):
    def __init__(self, terms: dict):
        self.terms = terms


class ChainGate:
    """Provider side of x402 over the contracts. Verifies off-chain updates against chain state and
    the gate's own memory of what it has already accepted, then settles in batches."""

    def __init__(self, ledger: ChainLedger, provider_key: str, offer: ChainOffer):
        self.L = ledger
        self.key = Account.from_key(provider_key)
        self.offer = offer
        self.latest: dict[str, dict] = {}  # "kind:id:account" -> update
        self.revenue_unsettled = 0

    def terms(self) -> dict:
        base = {"scheme": "foliant-channel", "network": self.L.network, "payTo": self.offer.provider,
                "asset": self.offer.token, "maxAmountRequired": str(self.offer.price_per_unit), "unit": self.offer.unit,
                "offerId": self.offer.id, "contracts": {"accounts": self.L.accounts.address,
                                                        "channels": self.L.channels.address, "pools": self.L.pools.address}}
        accepts = [base]
        if self.offer.pool_id:
            accepts.append({**base, "scheme": "foliant-pool", "poolId": _hex(self.offer.pool_id)})
        return {"x402Version": X402_VERSION, "accepts": accepts, "error": "payment required"}

    def verify(self, header: Optional[str]) -> ChainPayment:
        if not header:
            raise PaymentRequired(self.terms())
        try:
            p = _unb64(header)
            scheme, u = p["scheme"], p["update"]
            obj_id, acct = bytes.fromhex(u["id"][2:]), bytes.fromhex(u["account"][2:])
            if scheme == "foliant-channel":
                ch = self.L.channel(obj_id)
                if ch["payee"].lower() != self.offer.provider.lower() or ch["closed"] or ch["closing_at"] \
                        or ch["token"].lower() != self.offer.token.lower() or ch["payer"] != u["account"]:
                    raise FoliantError("channel not payable to this provider")
                if self.L.recover_channel_update(u).lower() != ch["signer"].lower():
                    raise FoliantError("bad update signature")
                deposit, onchain_bal = ch["deposit"], ch["paid"]
            elif scheme == "foliant-pool":
                if self.offer.pool_id is None or obj_id != self.offer.pool_id:
                    raise FoliantError("pool not run by this provider")
                claim = self.L.claim(obj_id, acct)
                if claim is None or claim["exited"] or claim["exit_at"]:
                    raise FoliantError("not an active member")
                if u.get("epoch") != claim["epoch"]:
                    raise FoliantError("update epoch does not match the claim")
                if self.L.recover_pool_update(u).lower() != claim["signer"].lower():
                    raise FoliantError("bad update signature")
                deposit, onchain_bal = claim["deposit"], claim["paid"]
            else:
                raise FoliantError(f"unknown scheme {scheme}")
            if not (isinstance(u["seq"], int) and isinstance(u["balance"], int)) or u["seq"] < 0 or u["balance"] < 0:
                raise FoliantError("seq and balance must be non-negative integers")
            key = f"{scheme}:{u['id']}:{u['account']}"
            prev = self.latest.get(key)
            last_bal = prev["balance"] if prev else onchain_bal
            if u["balance"] <= last_bal:
                raise FoliantError("stale update")
            if u["balance"] > deposit:
                raise FoliantError("update exceeds deposit")
            paid = u["balance"] - last_bal
            if paid < self.offer.price_per_unit:
                raise FoliantError(f"underpaid: {paid} < {self.offer.price_per_unit}")
            self.latest[key] = u
            self.revenue_unsettled += paid
            return ChainPayment(scheme, obj_id, acct, u, paid)
        except (KeyError, ValueError, TypeError, FoliantError) as e:
            raise PaymentRequired({**self.terms(), "error": str(e)})

    def dependency(self):
        def _dep(request: Request) -> ChainPayment:
            return self.verify(request.headers.get(HDR_PAYMENT))
        return _dep

    def receipt(self, payment: ChainPayment, request_body: bytes, response_body: bytes) -> str:
        body = {"updateId": Web3.keccak(text=json.dumps(payment.update, sort_keys=True)).hex(),
                "requestHash": Web3.keccak(request_body).hex(), "responseHash": Web3.keccak(response_body).hex(),
                "offerId": self.offer.id}
        sig = self.key.sign_message(encode_defunct(text=json.dumps(body, sort_keys=True))).signature
        return _b64({"body": body, "signer": {"scheme": "eip191", "key": self.key.address}, "signature": _hex(sig)})

    def _send(self, fn) -> dict:
        w3 = self.L.w3
        tx = fn.build_transaction({"from": self.key.address, "nonce": w3.eth.get_transaction_count(self.key.address),
                                   "chainId": self.L.chain_id})
        h = w3.eth.send_raw_transaction(self.key.sign_transaction(tx).raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h)
        if r["status"] != 1:
            raise FoliantError(f"settlement reverted: {h.hex()}")
        return r

    def create_pool(self, timeout_secs: int = 3600, salt: int = 0) -> bytes:
        self._send(self.L.pools.functions.create(Web3.to_checksum_address(self.offer.token), timeout_secs, salt))
        pid = self.L.pools.functions.poolId(self.key.address, salt).call()
        self.offer.pool_id = pid
        return pid

    def settle(self) -> tuple[int, list[str]]:
        """Step 5: one transaction per channel with a new update, one for the whole pool. Returns (total, tx hashes)."""
        total, txs = 0, []
        pool_updates: list[tuple] = []
        for key, u in list(self.latest.items()):
            scheme, obj_hex, _ = key.split(":", 2)
            obj_id = bytes.fromhex(obj_hex[2:])
            if scheme == "foliant-channel":
                ch = self.L.channel(obj_id)
                if u["balance"] <= ch["paid"]:
                    continue
                r = self._send(self.L.channels.functions.settle(obj_id, u["seq"], u["balance"], bytes.fromhex(u["sig"][2:])))
                total += u["balance"] - ch["paid"]
                txs.append(r["transactionHash"].hex())
            else:
                pool_updates.append((bytes.fromhex(u["account"][2:]), u["seq"], u["balance"], bytes.fromhex(u["sig"][2:])))
        if pool_updates:
            before = self.L.token(self.offer.token).functions.balanceOf(self.key.address).call()
            r = self._send(self.L.pools.functions.settle(self.offer.pool_id, pool_updates))
            after = self.L.token(self.offer.token).functions.balanceOf(self.key.address).call()
            total += after - before
            txs.append(r["transactionHash"].hex())
        self.revenue_unsettled = 0
        return total, txs


# --------------------------------------------------------------------------- agent HTTP client


class ChainHttpClient:
    """Agent side over HTTP: retries a 402 with a payment, opening a channel or joining the pool on demand."""

    def __init__(self, agent: ChainAgent, http, *, default_deposit: int, prefer_pool: bool = True,
                 timeout_secs: int = 3600):
        self.agent, self.http = agent, http
        self.default_deposit, self.prefer_pool, self.timeout_secs = default_deposit, prefer_pool, timeout_secs
        self.receipts: list[dict] = []

    def _choose(self, accepts: list[dict]) -> dict:
        if self.prefer_pool:
            for a in accepts:
                if a["scheme"] == "foliant-pool":
                    return a
        for a in accepts:
            if a["scheme"] == "foliant-channel":
                return a
        raise FoliantError("no Foliant scheme in the 402 terms")

    def _payment_for(self, term: dict) -> str:
        price = int(term["maxAmountRequired"])
        agent = self.agent
        if term["scheme"] == "foliant-pool":
            pid = bytes.fromhex(term["poolId"][2:])
            claim = agent.L.claim(pid, agent.account_id)
            if claim is None or claim["exited"]:
                agent.join_pool(pid, self.default_deposit)
            return _b64({"scheme": "foliant-pool", "id": term["poolId"], "update": agent.pay_pool(pid, price)})
        # channel: reuse the last open one to this payee with room, else open another
        cid = None
        for k, u in agent.latest.items():
            if u["kind"] != "channel":
                continue
            ch = agent.L.channel(k)
            if ch["payee"].lower() == term["payTo"].lower() and not ch["closed"] and not ch["closing_at"] \
                    and u["balance"] + price <= ch["deposit"]:
                cid = k
                break
        if cid is None:
            cid = agent.open_channel(term["payTo"], term["asset"], self.default_deposit, self.timeout_secs,
                                     salt=agent.L.now)
        return _b64({"scheme": "foliant-channel", "id": _hex(cid), "update": agent.pay_channel(cid, price)})

    def request(self, method: str, url: str, **kw):
        r = self.http.request(method, url, **kw)
        if r.status_code != 402:
            return r
        term = self._choose(r.json()["accepts"])
        headers = {**kw.pop("headers", {}), HDR_PAYMENT: self._payment_for(term)}
        r = self.http.request(method, url, headers=headers, **kw)
        if HDR_RECEIPT in r.headers:
            self.receipts.append(_unb64(r.headers[HDR_RECEIPT]))
        return r


# --------------------------------------------------------------------------- local deployment (tests, demos)


def deploy_local(w3: Web3, deployer_key: str) -> dict:
    """Deploy the three contracts and a mintable test token to the chain behind `w3`; lock the modules."""
    acct = Account.from_key(deployer_key)

    def params():
        return {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id}

    def send(tx, what: str):
        h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        r = w3.eth.wait_for_transaction_receipt(h)
        if r["status"] != 1:
            raise FoliantError(f"{what} reverted: {h.hex()}")
        return r

    def deploy(name, *args):
        art = _abi(name)
        c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
        r = send(c.constructor(*args).build_transaction(params()), f"deploy {name}")
        return w3.eth.contract(address=r["contractAddress"], abi=art["abi"])

    accounts = deploy("AgentAccounts")
    channels = deploy("PaymentChannels", accounts.address)
    pools = deploy("Pools", accounts.address)
    send(accounts.functions.lockModules([channels.address, pools.address]).build_transaction(params()), "lockModules")
    token = deploy("MockERC20")
    return {"accounts": accounts.address, "channels": channels.address, "pools": pools.address, "token": token.address}
