# Molt Family Dashboard

Live: **https://jimmolt.github.io/Family-Dashboard/**

Kitchen display: clock, Spalding weather, family calendars, photo slideshow,
today's school breakfast & lunch (Spalding site), Chargers sports and school events.
GitHub rebuilds it every hour. Family calendars and photos are **encrypted** —
the page asks for the passphrase once per device (or the kiosk link carries it).

## Settings (repo → Settings → Secrets and variables → Actions)
| Secret | What |
|---|---|
| `DASH_PASSPHRASE` | Unlocks calendars + photos |
| `DROPBOX_LINK` | Shared link to the Dropbox **Kitchen Display** folder |
| `FAMILY_CALENDARS` | `[{"name":"Jim","color":"#4f8cff","url":"<secret iCal address>"}, ...]` |

Get each calendar's link in Google Calendar → Settings → (calendar) →
Integrate calendar → **Secret address in iCal format**.

Other settings (weather spot, school calendar, lunch cutoff, photo timing) are in `config.json`.

## Every month
Put the **SP Site <Month> Menu** PDF in Dropbox › Kitchen Display › **Lunch**.

## Raspberry Pi Zero 2 W kiosk
Flash Raspberry Pi OS Lite (64-bit) with Wi-Fi + SSH, then on the Pi:

    curl -sLO https://jimmolt.github.io/Family-Dashboard/setup-kiosk.sh
    bash setup-kiosk.sh 'YOUR-PASSPHRASE'
    sudo reboot

## On any other screen
Open the link and enter the passphrase (remembered on that device).
Shift+L brings the passphrase box back.

Rebuild right now: Actions tab → Build dashboard → Run workflow.
