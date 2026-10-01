# Running the demo server

What this puts on a box: the metered demo API (`demo/serve_chain.py`) on Avalanche Fuji, behind
nginx with TLS, as an unprivileged `foliant` user, with a timer that settles every ten minutes.
Reference host: AWS `t4g.micro` (arm64, 1 GiB), Ubuntu 24.04. Anything comparable will do.

The files here are templates; the commands below are what to run on the box.

## 1. Base packages and a swap file

A 1 GiB box with no swap will kill the server rather than slow down, and `pip` is the step most
likely to need the room.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip nginx curl git
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. The service user and the code

The user owns nothing but the code and the state directory, and cannot log in.

```bash
sudo useradd --system --home /opt/foliant --shell /usr/sbin/nologin foliant
sudo mkdir -p /opt/foliant /var/lib/foliant /etc/foliant
sudo chown foliant:foliant /opt/foliant /var/lib/foliant
sudo -u foliant git clone https://github.com/gazoy/concord /opt/foliant/concord
sudo -u foliant python3 -m venv /opt/foliant/venv
sudo -u foliant /opt/foliant/venv/bin/pip install -q -r /opt/foliant/concord/requirements.txt
```

## 3. The environment file

Both keys are testnet keys for wallets used by nothing else.

```bash
sudo cp /opt/foliant/concord/deploy/demo.env.example /etc/foliant/demo.env
sudo nano /etc/foliant/demo.env          # fill in PROVIDER_KEY, FOLIANT_TAP_KEY, FOLIANT_SETTLE_KEY
sudo chown root:foliant /etc/foliant/demo.env
sudo chmod 640 /etc/foliant/demo.env
```

`openssl rand -hex 32` gives a settle key. Fund the tap wallet from the Core faucet
(<https://core.app/tools/testnet-faucet/>); 1 AVAX covers about forty visitors.

## 4. The service and the settle timer

```bash
cd /opt/foliant/concord/deploy
sudo cp foliant-demo.service foliant-settle.service foliant-settle.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foliant-demo foliant-settle.timer
systemctl status foliant-demo --no-pager
curl -s localhost:8402/chain | head -c 400
```

The first start creates the pool on chain, so it takes a few seconds and needs the provider wallet
to hold a little AVAX for gas.

## 5. nginx and TLS

DNS for the name must already resolve to this box, or certbot's challenge fails.

```bash
sudo cp fuji.foliant.network.conf /etc/nginx/sites-available/fuji.foliant.network
sudo ln -s /etc/nginx/sites-available/fuji.foliant.network /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d fuji.foliant.network --agree-tos -m gaz@garethoyston.com --redirect
```

Renewal is handled by the timer certbot installs; `sudo certbot renew --dry-run` confirms it.

## 6. Check it from somewhere else

```bash
curl -s https://fuji.foliant.network/chain | python3 -m json.tool
curl -s -X POST https://fuji.foliant.network/tap -H 'content-type: application/json' \
     -d '{"address":"0x<a fresh address>"}'
```

`/chain` names the contracts, the token, the pool and the tap's balance. A successful tap returns
two transaction hashes; look them up on <https://testnet.snowtrace.io>.

## Afterwards

- **Updating**: `sudo -u foliant git -C /opt/foliant/concord pull && sudo systemctl restart foliant-demo`.
  The server settles what it holds before stopping, so a restart loses nothing.
- **Refilling the tap**: `/chain` reports `tap.balanceWei`; top the wallet up from the faucet.
- **Logs**: `journalctl -u foliant-demo -f`, and `/var/log/nginx/foliant-*.log`.
- **Limits**: the tap gives each client three addresses a day, twenty an hour and a hundred a day
  overall. `demo/TAP-REVIEW.md` and `demo/TAP-AUDIT-*.md` record why each of those exists.
