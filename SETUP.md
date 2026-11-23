# Setup: two accounts, two clients

## The machine

Use a **Windows 10 or 11 PC directly. Don't use a virtual machine.**

The sniper has to run in the same Windows session as both game clients,
because it detects the game process, brings the buyer window forward and
listens for a global hotkey. A VM adds a layer between all of those and
costs graphics performance. Two clients is a normal setup on one PC.

| | Minimum | Recommended |
|---|---|---|
| RAM | 16 GB | 32 GB |
| GPU | runs PoE comfortably | 8 GB VRAM or more |
| Network | wired | wired |

The memory has to cover two clients, Chrome with about 5 trade tabs, and the app.

**On a Mac:** Apple Silicon has no Boot Camp. Parallels with Windows 11 on
ARM could in principle run it, but it is untested here and PoE would run
under x64 emulation. Use a real Windows PC.

## One-time setup

1. **Two accounts.**
   - **A is the buyer.** It holds the currency and does every purchase.
   - **B** is the account you play on.
2. **Install the app.**
   - Download `ValdoSniper-Setup.exe` from
     https://github.com/andrewli8/PoEValdoAssistant/releases/tag/build-latest
   - Run it. Windows SmartScreen warns because the installer isn't code-signed: click **More info**, then **Run anyway**.
   - The app itself runs on python.org's `pythonw.exe`, which is signed, so Defender has no packed exe to flag. If anything is still blocked, see **If Windows says it's malicious** below.
   - The installer needs no admin rights and installs to `%LOCALAPPDATA%\Programs\ValdoSniper`. Your `config.yaml`, `.env`, logs and saved searches live there too.
3. **Leave "Start Valdo Sniper now" ticked** on the last page.
4. Later, start it from the Start menu: **Valdo Sniper**.
5. **Open the sniper Chrome:** in the overlay, click **Open the sniper Chrome**.
   - The app launches its own Chrome with its own profile and the right flags, so there's no shortcut to make.
   - It's a fresh Chrome with no extensions yet, and it opens a setup page with these steps:
     - Install Tampermonkey.
     - Go to `chrome://extensions`, then *Tampermonkey*, then *Details*, and turn on **Allow User Scripts**.
     - Install the browser script.
     - Log into pathofexile.com as **account A only**.
     - Go to Settings, then *Performance*: add `pathofexile.com` to *Always keep these sites active*, and turn off Energy Saver.
6. **Import your searches.** In Better Trading, open your Valdo folder's ⋯ menu and choose **Export**.
   - The import box opens by itself shortly after launch while nothing is saved and no tab is connected. It fills in from your clipboard. Otherwise click **Import Better Trading searches**.
   - Click **Save & open tabs**. For several folders, put one export per line.
   - The searches are saved in `searches.txt` for next time, and their prices refresh on their own every 30 minutes.
   - To skip some items, open ⚙ and untick them under *Live searches*. Unticked searches are neither opened nor priced, which also leaves more of the search limit for the rest.
7. **Tabs open by themselves.** Each search opens as a live tab for the current league: 6 seconds apart for the first 10, then 15 seconds apart, so you never hit the trade site's search limit. Price checks pause while tabs open.
   - When Chrome asks whether the site may access devices on your local network, click **Allow**.

## If Windows says it's malicious

This is a false positive on an unsigned download. It's a known problem for small open-source apps that can't pay for a code-signing certificate. Only override it for files you downloaded from the `build-latest` release on this GitHub page.

**Chrome blocks the download** ("dangerous file" or "virus detected"):
open the downloads list (Ctrl+J), click **⋮** on the file, then **Keep** or **Download dangerous file**.

**Defender deleted the installer:**

1. Open **Windows Security**, then **Virus & threat protection**, then **Protection history**.
2. Open the Valdo Sniper entry. Check that the file path is your Downloads folder.
3. Choose **Actions**, then **Allow on device**, and download it again.

**Easiest way round it: the portable zip** (no installer to block):

1. From the same release page, download `ValdoSniper-portable.zip`.
2. Before unzipping, right-click the zip, choose **Properties**, tick **Unblock**, then click **OK**. Skip this and Windows warns on every start.
3. Unzip it anywhere you can write to, for example `Documents\ValdoSniper`.
4. Double-click **`Valdo Sniper.cmd`**.

Your `config.yaml`, `.env` and saved searches stay in that folder. To update, unzip a new copy and copy those files across.

You can also report the false positive to Microsoft at https://www.microsoft.com/wdsi/filesubmission (choose "Incorrectly detected as malware"). They usually clear it within a few days.

## Install from source (no exe)

Use this if both the installer and the zip are blocked. It runs the same code with a normal
Python, so there's no packed exe for Defender to misjudge.

1. Install **Python 3.12** from python.org and tick **Add python.exe to PATH**.
   Or run `winget install -e --id Python.Python.3.12`.
2. Download and unzip the branch ZIP:
   https://github.com/andrewli8/PoEValdoAssistant/archive/refs/heads/feat/two-client-assist.zip
3. In the folder, copy `.env.example` to `.env`.
4. Double-click **`start.bat`**. The first run sets everything up in about a minute. Later runs start straight away.

Then continue from step 5 above.

## Every session

1. Start **Valdo Sniper** from the Start menu (`Valdo Sniper.cmd` for the portable zip, `start.bat` from source).
2. Missing live tabs reopen by themselves about 45 seconds after start. Tabs that are already open are skipped.
3. Start **client A** on account A and park it in your hideout.
4. Start **client B** on account B.
5. Click into client A and press **Ctrl+Alt+B**. The status strip shows `buyer ✓`.
6. Play on B. When an alert fires, press **`** (backtick). Client A travels and comes to the front.
7. In the seller's shop, press **Ctrl+F**, then **Ctrl+V**, to find the map. Buy it by hand.

If you restart client A, press Ctrl+Alt+B again.

## Check that it works

The status strip should read `N tabs · PoE running · buyer ✓ · Trade prices`.

| What you see | What to do |
|---|---|
| `0 tabs` | Tampermonkey is missing in the sniper Chrome, or Allow User Scripts is off. Click **Open the sniper Chrome** and check the setup page, then reload the trade tabs |
| Nothing opens | Chrome isn't installed in a standard place. Install Google Chrome normally, then try again |
| Console shows `ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS` | Lock icon, then *Site settings*, then allow *Local network access* for pathofexile.com, then reload |
| `update script` | Click it, then hard-reload the trade tabs (Ctrl+Shift+R) |
| `BUYER LOST` | Client A closed or restarted: click into it and press Ctrl+Alt+B |
| `TRADE API REFUSED US` | The trade site answered 401/403. If `.env` has a `POESESSID`, it expired: replace it or leave it empty. Otherwise the site is blocking you for now: wait |
| Hotkey does nothing | The game isn't detected, or no alert is live |

## Rules this follows

- One key press makes one click on the trade site.
- Nothing types or clicks inside either game client.
- You buy every item by hand.
- GGG staff forum posts allow two clients per PC, one account each. That isn't on an official rules page, so check their current stance yourself.
