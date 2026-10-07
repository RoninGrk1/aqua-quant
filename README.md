# Aqua-Quant

Paper terminal for stocks and indices. Public site: https://roningrk1.github.io/aqua-quant/

Prices come from the Yahoo Finance chart API. The wallet is a local ledger, not a broker account. The 60% figure is an entry filter, not a promised win rate.

## Run the terminal

```bash
git clone https://github.com/RoninGrk1/aqua-quant.git
cd aqua-quant
python3 server.py
```

Open http://127.0.0.1:8765

Python 3.10 or newer. No packages to install. Sign in with email and username, deposit paper USD, then arm the 4h 30m desk.

## Website

The Pages site lives in `website/`. GitHub Pages cannot host the terminal: the chart feed blocks browser requests, and deposits need the local server.

After the first push, open Settings, Pages, and set the source to GitHub Actions if the workflow has not been allowed yet.

## Service installs

- Linux: `deploy/aqua-quant.service`
- macOS: `deploy/com.aquaquant.terminal.plist`
- Windows: `powershell -ExecutionPolicy Bypass -File .\deploy\install-windows-task.ps1`
