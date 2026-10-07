# Aqua-Quant

One desk. Ten seats. Stocks and indices, 4-hour bias, 30-minute trigger.

This is the paper terminal that only takes a shot when the desk actually agrees. Real candles. No made-up prices. No broker, no bank wire, no magic.

Site: https://roningrk1.github.io/aqua-quant/
Repo: https://github.com/RoninGrk1/aqua-quant

## What it does

- Pulls live charts from the Yahoo Finance chart API. No key. If a feed dies, that symbol goes dark. Nothing gets invented.
- Ten seats read the tape: Tide, Current, Pulse, Drift, Swell, Depth, Flow, Shelf, Revert, and Lock.
- Lock is the bouncer. 4h and 30m have to agree, at least 6 of 9 seats have to line up, and the confluence score has to clear 60%. Miss any of that and the trade does not happen.
- Size is tiny on purpose: 0.05% of paper equity, stop at 1.2× the 30-minute ATR, target 1.8R.
- Arm a 4 hour 30 minute session. Scan every 40 seconds. Three positions max. If the session is down 1%, the desk stops.
- Wallet is paper. Deposit and withdraw stay in a local ledger. That cash does not leave your machine.

The 60% number is the door, not a promise. It does not mean the next trade wins 60% of the time.

## Fire it up

```bash
git clone https://github.com/RoninGrk1/aqua-quant.git
cd aqua-quant
python3 server.py
```

Open http://127.0.0.1:8765

Python 3.10 or newer. No pip, no extra packages. Make an account with email and username, drop in some paper USD (1,000 minimum to arm), then hit Arm 4h 30m.

## Leave it running

- Linux: `deploy/aqua-quant.service`
- macOS: `deploy/com.aquaquant.terminal.plist`
- Windows: `powershell -ExecutionPolicy Bypass -File .\deploy\install-windows-task.ps1`

## The website

`website/` is the public page. GitHub Pages can host that. It cannot host the terminal. Yahoo blocks the browser, and the wallet needs this server.

If the page is not up yet: Settings, Pages, source GitHub Actions, then re-run Publish website.

Not investment advice. Paper desk. Real prices. Small risk. That is the whole pitch.
