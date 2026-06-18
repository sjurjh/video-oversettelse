# Video oversettelse til norsk tale

Dette prosjektet lager en separat norsk AI-tale-fil for en lokal videofil, slik at videoen kan spilles av i VLC sammen med norsk lyd. Originalvideoen endres ikke.

Merk: Denne README-en er skrevet med ASCII-tegn der det er praktisk, fordi PowerShell og enkelte verktoy ellers kan vise norske tegn feil.

## Hva programmet gjor

`dub_to_norwegian.py` tar en lokal video (`.mp4`, `.mkv`, `.webm`, `.mov`) og lager:

- original lyd som komprimert arbeidsfil
- transkripsjon med tidsstempler
- norsk bokmal-oversettelse
- en TTS-lydfil per segment
- synkronisert sluttlyd:
  - `norwegian_voice.wav`
  - `norwegian_voice.mp3`
- en videofil med norsk lyd som ekstra lydspor:
  - `<video>_with_norwegian_audio.mp4` for MP4/MOV-kilder
  - `<video>_with_norwegian_audio.mkv` for andre kilder

Sluttlyden kan spilles i VLC med:

```powershell
vlc "video.mkv" --input-slave="norwegian_voice.mp3"
```

## Viktige filer

- `dub_to_norwegian.py` - hovedprogrammet.
- `start_ui.bat` - starter Tkinter-UI-en.
- `install_dependencies.bat` - installerer Python-pakker fra `requirements.txt`.
- `requirements.txt` - Python-avhengigheter: `openai`, `pydub`, `audioop-lts` for Python 3.13, `tqdm`, `python-dotenv`.
- `install_ffmpeg.bat` / `install_ffmpeg.ps1` - laster ned lokal FFmpeg Essentials-build til `tools/ffmpeg`.
- `install_winget.ps1` - forsok paa aa installere winget/App Installer via Microsofts GitHub-release.
- `.env` - inneholder bare peker til ekstern nokkelfil, ikke selve API-nokkelen.
- `.gitignore` - ignorerer `.env`, `tools/`, cache og outputmapper.

## API-nokkel

API-nokkelen skal ikke ligge i prosjektmappen. Den ligger i:

```text
C:\Users\sjur\AppData\Roaming\OpenAIKeys\openai.env
```

Format:

```env
OPENAI_API_KEY=din_ekte_openai_api_key_her
```

Prosjektets lokale `.env` peker dit:

```env
OPENAI_KEYS_FILE=C:\Users\sjur\AppData\Roaming\OpenAIKeys\openai.env
```

Ikke skriv ekte API-nokler i `README.md`, chat, commit-meldinger eller andre prosjektfiler.

## FFmpeg-status

Programmet leter automatisk etter FFmpeg her:

```text
PATH
C:\ffmpeg\bin
C:\Users\sjur\Documents\Video oversettelse\tools\ffmpeg\bin
```

Hvis UI-en sier at `ffmpeg` mangler, kjor:

```powershell
& "C:\Users\sjur\Documents\Video oversettelse\install_ffmpeg.bat"
```

Deretter start UI-en igjen:

```powershell
& "C:\Users\sjur\Documents\Video oversettelse\start_ui.bat"
```

## OpenAI-kvote

Sist kjente blokkering var:

```text
Error code: 429 insufficient_quota
```

Dette gjelder OpenAI API-billing/kvote, ikke ChatGPT-abonnement. Brukeren vurderer aa legge inn omtrent 10 USD i API-kreditt, som bor vaere nok til flere tester med 30-minutters videoer, saa lenge man ikke regenererer alt mange ganger med `--force`.

Sjekk:

- https://platform.openai.com/settings/organization/billing/overview
- https://platform.openai.com/usage
- https://platform.openai.com/settings/organization/limits

## Bruk

Start UI:

```powershell
& "C:\Users\sjur\Documents\Video oversettelse\start_ui.bat"
```

Kommandolinje:

```powershell
cd "C:\Users\sjur\Documents\Video oversettelse"
python dub_to_norwegian.py "C:\Videos\my_video.mkv" --output-dir "C:\Videos\norsk_lyd"
```

Hvis `python` ikke finnes i PATH, bruk `start_ui.bat`. Den prover baade `py`, `python` og vanlige Python-installasjonsmapper.

## Resume-logikk

Programmet er laget for aa kunne gjenoppta arbeid:

- Hvis `transcript_no.json` finnes, hopper det over transkripsjon og oversettelse med mindre `--force` brukes.
- Hvis segmentlydfiler finnes, lages de ikke paa nytt med mindre `--force-tts` brukes.
- `audio.mp3` er bare en arbeidsfil med originallyden fra videoen, ikke norsk tale.
- Sluttlyd og muxet video gjenbrukes hvis de allerede finnes og `--force`/`--force-tts` ikke brukes.
- Logg skrives til `process.log` i outputmappen.

## Ikke med i GitHub-repoet

Noen lokale filer og mapper ble bevisst ikke pushet til GitHub. Dette er styrt av `.gitignore`.

- `.env` - lokal konfigurasjonsfil som peker programmet mot en ekstern OpenAI-nokkelfil. Den skal ikke inneholde selve API-nokkelen i repoet.
- `tools/` - lokal FFmpeg-installasjon med kjorebare filer og tilhorende biblioteker. Den kan lastes ned igjen med `install_ffmpeg.bat`.
- `__pycache__/` og `*.pyc` - automatisk genererte Python-cachefiler.
- `*_norwegian_voice/` - genererte outputmapper per video. De kan inneholde ekstrahert originallyd, transkripsjoner, norsk oversettelse, en TTS-lydfil per segment, sluttlyd som WAV/MP3, muxet video med norsk lydspor og prosesslogg.
- `.git/` - lokal Git-database med commit-historikk og remote-oppsett. Den er ikke en del av filene som pushes.

## Naa-vaerende teknisk status

- Python-scriptet kompilerer syntaktisk.
- Tkinter-UI er lagt til.
- Lokal FFmpeg-installasjon er stottet via `tools/ffmpeg`.
- API-nokkel er flyttet ut av prosjektmappen til AppData.
- `.gitignore` er lagt til for prosjektet og nokkelmappen.
- Full videopipeline er ikke bekreftet ferdigkjort ennaa paa grunn av API-kvote.

## Neste sannsynlige steg

1. Fyll paa OpenAI API-kreditt eller sjekk prosjekt-/org-grenser.
2. Kjor `install_ffmpeg.bat` hvis `tools/ffmpeg/bin/ffmpeg.exe` ikke finnes.
3. Start `start_ui.bat`.
4. Velg en kort testvideo forst, gjerne 1-3 minutter, for aa verifisere hele flyten billig.
5. Naar kort test fungerer, prov lengre video.
