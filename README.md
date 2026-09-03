# Glaze — eye tracker 3D pe Raspberry Pi

Port al trackerului [`Orlosky3DEyeTracker.py`](https://github.com/JEOresearch/EyeTracker/tree/main/3DTracker)
(Jason Orlosky, JEOresearch) pentru **Raspberry Pi 3A+ cu Raspberry Pi OS Lite**,
fără interfață grafică pe Pi, cu output pe web în browserul de pe laptop.

Algoritmul e al lui — punctul cel mai întunecat, trei praguri, alegerea
elipsei celei mai bune, razele pupilei intersectate ca să estimeze centrul
sferei ochiului, apoi vectorul 3D de privire. Eu am schimbat doar ce trebuia
ca să meargă pe Pi.

> Nota: în tutorial linkul e către `3DTrackerDIY`, folder care nu există în
> repo. Codul real e în `3DTracker/Orlosky3DEyeTracker.py` — ăla e portat aici.
> Copia originală e păstrată în `upstream/` ca referință.

---

## 1. Hardware

| Componentă | Rol | Config |
|---|---|---|
| Raspberry Pi 3A+ (512 MB) | rulează trackerul | Pi OS Lite, headless |
| Cameră USB IR (GC0308) | camera de ochi | `--eye usb:0` |
| Pi Camera v2 (CSI, IMX219) | camera de scenă | `--scene csi` |
| Laptop | afișaj + calibrare | browser, aceeași rețea |

Rolurile se pot inversa (`--eye csi --scene usb:0`) dacă montezi camera CSI
lângă ochi.

**Siguranță IR:** camera GC0308 are LED-uri infraroșii care stau la câțiva
centimetri de ochi. Citește [nota de siguranță IR](https://www.jeoresearch.com/irsafety.html)
a autorului înainte s-o porți mult timp; dacă modulul are LED-uri puternice,
mai bine le acoperi parțial sau le alimentezi prin rezistență mai mare.

---

## 2. Instalare pe Pi (Pi OS Lite)

```bash
git clone <repo> ~/Moviris_glaze && cd ~/Moviris_glaze && chmod +x install_pi.sh && ./install_pi.sh
```

Scriptul instalează prin **apt**, nu prin pip: `pip install opencv-python` pe un
Pi 3A+ cu 512 MB fie compilează ore întregi, fie rămâne fără memorie.

```bash
sudo apt install -y python3-opencv python3-numpy python3-picamera2 v4l-utils
```

Camera CSI trebuie activată în `/boot/firmware/config.txt` (pe Bookworm e
detectată automat; dacă nu, adaugă `camera_auto_detect=1` și repornește).

Verifică ce vede Pi-ul:

```bash
python3 tools/list_cameras.py
```

---

## 3. Pornire

```bash
python3 -m glaze --preset lite
```

Apoi, pe laptop: `http://<ip-ul-pi-ului>:8000/`

### Camerele se detectează automat

Implicit, `--eye` și `--scene` sunt pe `auto`: la pornire, aplicația verifică
ce noduri `/dev/videoN` chiar livrează cadre și le atribuie în ordine — prima
= ochi, a doua = scenă.

De ce: numerotarea `/dev/videoN` se schimbă când muți o cameră în alt port USB,
sau chiar doar în funcție de ce a văzut kernelul prima la boot. Indexuri fixe
în fișierul de service se strică la fiecare replug.

Cele două camere sunt identice ca model, deci nimic din sistem nu poate ști
care e care. Dacă imaginile apar inversate, apeși **`inversează ochi/scenă`**
în dashboard — alegerea se salvează în `runtime.json` și se păstrează la
repornire. Butonul `re-detectează camere` reface scanarea fără să repornești
serviciul (util după ce ai mutat un cablu).

Poți în continuare forța explicit, iar valoarea explicită bate auto-detecția:

```bash
python3 -m glaze --eye usb:0 --scene csi
```

### Setările se salvează

Tot ce schimbi din `/settings` (și inversarea camerelor) se scrie în
`runtime.json` și se reîncarcă la pornire. Doar o listă albă de parametri e
persistabilă — porturi și căi rămân controlate din linia de comandă, ca o
valoare salvată greșit să nu poată face serviciul imposibil de pornit.

Dacă ai stricat ceva din interfață și vrei să te întorci la zero:

```bash
python3 -m glaze --reset-settings
```

Presetări:

| Preset | Captură → procesare | Pentru |
|---|---|---|
| `lite` | 320×240 → 320×240 | Pi 3A+ / Pi Zero 2 W |
| `balanced` (implicit) | 640×480 → 320×240 | Pi 3A+ când ai CPU liber |
| `quality` | 640×480 → 640×480 | Pi 4/5 sau laptop |

Dacă îți alegi singur rezoluția: ține procesarea **egală cu captura, sau exact
jumătate/sfert din ea**. La dimensiunile astea redimensionarea costă mai mult
decât detecția propriu-zisă, iar o scalare cu factor ne-întreg pierde calea
rapidă `INTER_AREA` din OpenCV. Măsurat: 640×480 → 256×192 iese **mai lent**
(1.57 ms) decât 640×480 → 320×240 (1.04 ms), iar captura direct la 320×240
scapă complet de cost (0.91 ms).

Opțiuni utile:

```bash
python3 -m glaze --help

--eye usb:0 | csi | /cale/video.mp4   # sursa camerei de ochi
--scene csi | usb:1 | ""              # "" dezactivează camera de scenă
--rotate 0|90|180|270                 # dacă montezi camera strâmb
--no-flip                             # originalul întoarce imaginea pe verticală
--no-overlay                          # fără desen peste imagine (mai rapid)
--write-gaze-file                     # scrie gaze_vector.txt ca în original
--udp 192.168.1.20:9999               # trimite vectorul pe UDP către laptop
--save-config config.json             # salvează configul curent
```

### Pornire automată la boot

```bash
sudo cp glaze.service /etc/systemd/system/
sudo nano /etc/systemd/system/glaze.service   # verifică User= și WorkingDirectory=
sudo systemctl enable --now glaze
journalctl -u glaze -f
```

---

## 4. Interfața web

- **stânga**: camera de ochi cu overlay-ul original (elipsa pupilei, sfera
  ochiului, linia de privire, vectorul 3D scris jos)
- **dreapta**: camera de scenă cu un reticul acolo unde te uiți (după calibrare)
- **dreapta jos**: toate valorile live + controalele

Tastele din scriptul original au devenit butoane:

| Original | Web |
|---|---|
| `f` — fixează sfera ochiului | `lock sphere` |
| `e` — capturează elipse | `trace ellipses` |
| `c` — șterge elipsele | `clear traces` |
| `q` / spațiu | închizi tabul / `Ctrl+C` pe Pi |

### Calibrare

Două variante, ambele produc aceeași mapare (privire → coordonate 0..1):

1. **`calibrate to this screen`** — se deschide un overlay pe tot ecranul
   laptopului cu 9 puncte. Te uiți la punct, apeși **SPACE**, se trece la
   următorul. La final se salvează automat și poți porni `show gaze cursor`,
   care mișcă un cerc pe pagină după privirea ta.
2. **`calibrate on scene view`** — te uiți la un obiect real, apoi dai click pe
   el în imaginea camerei de scenă. Minim 6 puncte pentru potrivirea pătratică.

Calibrarea se salvează în `calibration.json` și se încarcă singură la pornire.

**Important înainte de calibrare:** lasă trackerul 10–20 de secunde în care
te uiți în jur (stânga-dreapta-sus-jos). Are nevoie de raze din direcții
diferite ca să estimeze centrul sferei ochiului (`rays` ajunge la 100,
`sphere radius` se stabilizează). Apoi apasă `lock sphere` și calibrează —
altfel modelul se mai mișcă sub tine în timpul calibrării.

### Problema cu genele (important)

Detectorul original raporta **întotdeauna** cea mai bună elipsă găsită, oricât
de proastă — nu exista niciun prag sub care să spună "asta nu e o pupilă". Pe
clipul de test asta însemna `ok=True` pe **toate** cele 1033 de cadre, inclusiv
când ochiul era închis și elipsa stătea pe pleoapă sau pe un smoc de gene.
Efect secundar: detecția de clipit nu se putea declanșa niciodată, pentru că
pupila nu se "pierdea" niciodată.

Ce am adăugat ca să respingă genele:

| Filtru | Ce respinge |
|---|---|
| `lash_open_iterations` | Curățare morfologică (opening) care șterge structurile subțiri *înainte* de dilatare — genele sunt subțiri, pupila nu. Fără el, dilatarea le îngroșa în blob-uri cât o pupilă. |
| `pupil_min_confidence` | Potriviri slabe = ochi închis, nu pupilă |
| `pupil_min_circularity` | Genele și cutele pleoapei sunt alungite, pupila e rotundă |
| `pupil_max_area_fraction` | Umbre mari de pleoapă care acopereau un sfert din cadru |
| `pupil_max_jump_fraction` | Pupila nu se teleportează: o detecție slabă care a sărit în cealaltă parte a cadrului e o agățare de gene |
| `pupil_max_eye_radius_fraction` | Odată ce modelul sferei există, pupila stă **pe** globul ocular. Ceva mai departe de centru decât raza sferei e în afara ochiului — fizic imposibil pentru o pupilă |

Pe clipul de test, rata de `ok` a scăzut de la 1.000 la 0.881 și au apărut 24
de clipiri detectate în 43s (26/min, plauzibil pentru un om).

**Toate pragurile astea sunt reglabile live** din `/settings`, iar pagina
afișează metricile cadrului curent (rotunjime, arie) și motivul respingerii —
folosește-le ca să calibrezi pe camera ta, nu pe a mea:

- vezi elipsa pe gene → **crește** `pupil_min_confidence` / `pupil_min_circularity`
- pierde pupila prea des → **scade-le**

### Detecție clipit (`blinking`, `triple_blink`)

Trackerul detectează clipitul din simpla absență a pupilei: dacă elipsa nu e
găsită pentru o durată scurtă (implicit 60–400ms), e clipit; dacă durează mai
mult (ochelari mișcați, ochiul iese din cadru), e ignorat ca pierdere reală de
tracking, nu clipit. Trei clipiri în aceeași fereastră de timp (implicit
1500ms) declanșează `triple_blink: true` pentru un frame în `/api/state` și
`/events` — un gest simplu, ușor de folosit ca declanșator pentru alte
funcții (ex. un mod de selecție într-o interfață AAC).

Gesturile sunt **dezactivate** primele `blink_warmup_s` secunde (implicit 8)
după fiecare `reset model` — atât are nevoie modelul sferei ochiului să se
stabilizeze, ca să nu declanșeze fals în timp ce tracking-ul abia pornește.
Starea `armed` din `/api/state` arată dacă gesturile sunt active acum.

Toți parametrii ăștia (praguri de durată, fereastră, warmup) sunt reglabili
live din [`/settings`](#pagina-de-setari), fără restart.

### Gesturi (`/gestures`)

Trackerul transformă privirea într-un flux de simboluri discrete, iar tu
definești tipare peste ele:

| Simbol | Înseamnă |
|---|---|
| `L` `R` `U` `D` | privirea a intrat în zona stânga/dreapta/sus/jos |
| `C` | privirea a revenit în centru |
| `B` | o clipire confirmată |

Un gest = o secvență de simboluri + un timp maxim în care trebuie completată +
o pauză după declanșare (ca să nu se repete). Exemplu: `L R L R` în 4000ms, sau
`B B B` în 1500ms.

O direcție emite un simbol **o singură dată** la intrarea în zonă — privirea
trebuie să treacă înapoi prin centru înainte ca aceeași direcție să se poată
declanșa din nou (altfel o privire parcată lângă prag ar emite simboluri la
fiecare cadru). Trecerile prin centru sunt ignorate automat la potrivire, dacă
nu pui `C` explicit în tipar — altfel `L R L R` n-ar merge niciodată, pentru
că stream-ul real e `L C R C L C R`.

Acțiuni disponibile pentru fiecare gest: întreabă modelul AI ce obiect e,
resetează modelul, blochează/deblochează sfera, salvează calibrarea, trimite un
POST la un URL al tău (pentru integrări proprii), sau nimic (util cât testezi
un tipar nou — vezi în pagină când se declanșează, fără să facă ceva).

Gesturile se salvează în `gestures.json`.

### "La ce mă uit?" (model de viziune)

Acțiunea `identify_object` face o poză cu camera de scenă, desenează un cerc
în punctul calibrat unde te uiți — cu raza dată de eroarea RMS a propriei tale
calibrări, deci cercul e mai mare când calibrarea e mai slabă — și o trimite la
Gemini, care răspunde cu JSON:

```json
{"objects": [{"name": "cană", "probability": 0.72},
             {"name": "pahar", "probability": 0.21}],
 "scene": "birou cu laptop și cană"}
```

Implicit folosește `gemini-3.5-flash-lite` (cel mai ieftin model cu vedere).
Apelul rulează pe un thread separat, deci o rețea lentă nu blochează niciodată
bucla de tracking.

**Google retrage periodic nume de modele.** Dacă primești o eroare `404`,
mesajul de la API îți spune exact ce model să folosești în loc — îl schimbi
direct în `/settings` → "Model AI viziune", fără să atingi codul.

Cheia API **nu** se pune în config (pagina de setări o poate citi înapoi).
Pune-o într-una din astea:

```bash
export GEMINI_API_KEY="cheia-ta"          # sau, permanent pentru serviciu:
echo "cheia-ta" > ~/MovirisGlazeTracker/gemini_key.txt
```

Pentru serviciul systemd, adaugă în `/etc/systemd/system/glaze.service`:

```
Environment=GEMINI_API_KEY=cheia-ta
```

`gemini_key.txt` e în `.gitignore`, deci nu ajunge din greșeală pe GitHub.

### Pagina de setări {#pagina-de-setari}

Link "settings & power" din header-ul principal, sau direct `http://<ip>:8000/settings`.
Toți parametrii de mai sus (cameră, stream, detecție pupilă, clipit) sunt
acolo, cu aplicare instant din browser, fără SSH și fără restart de serviciu —
utile: praguri de detecție, fps-uri, ferestre de calibrare a sferei ochiului.

Excepție: rezoluția de procesare (`proc_width`/`proc_height`) și sursele de
cameră (`--eye`/`--scene`) NU sunt live-reglabile — schimbă comportamentul
capturii de la cameră, care are nevoie de reinițializare completă. Alea rămân
din linia de comandă / fișierul de service, urmate de `restart tracker`.

Pagina mai are trei butoane de alimentare:
- **restart tracker** — repornește doar procesul `glaze` (echivalent
  `systemctl restart glaze`), util după ce ai schimbat manual `ExecStart`.
- **reboot Pi** — repornește tot Raspberry Pi-ul.
- **oprește Pi** — shutdown complet; repornești manual de la alimentare.

Astea rulează comenzi `sudo systemctl ...` pe Pi, deci au nevoie de drepturi
NOPASSWD explicite. Rulează o singură dată pe Pi:

```bash
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart glaze, /bin/systemctl reboot, /bin/systemctl poweroff" | sudo tee /etc/sudoers.d/glaze-power
```

Fără asta, butoanele răspund `ok: true` (comanda a pornit) dar nu se întâmplă
nimic — `sudo` cere parolă în fundal și eșuează silențios, pentru că serverul
nu așteaptă răspunsul procesului (nu poate: `reboot`/`poweroff` omoară chiar
procesul care a primit cererea HTTP).

---

## 5. Cum scoți datele în programul tău

| Sursă | Format |
|---|---|
| `GET /api/state` | JSON complet, o dată |
| `GET /events` | Server-Sent Events, ~10 Hz |
| `GET /gaze_vector.txt` | cele 6 valori ca în original |
| `--write-gaze-file` | fișierul `gaze_vector.txt` local pe Pi |
| `--udp host:port` | o linie CSV per frame |

Formatul de 6 valori e identic cu al originalului:

```
x_origin,y_origin,z_origin,x_direction,y_direction,z_direction
```

Exemplu de citire de pe laptop:

```python
import json, urllib.request
with urllib.request.urlopen("http://192.168.1.50:8000/events") as stream:
    for line in stream:
        if line.startswith(b"data: "):
            state = json.loads(line[6:])
            print(state["gaze_direction"], state["scene_point"])
```

Pe lângă cele 6 valori, `state` conține `gaze_normalized`: poziția pupilei față
de centrul sferei, împărțită la raza sferei. Ăsta e semnalul stabil, independent
de rezoluție — el se folosește la calibrare, și el e cel mai bun de folosit dacă
îți construiești propriul mapping.

---

## 6. Ce am schimbat față de codul original

**Scos de tot (nu au ce căuta pe un Pi Lite):**
- fereastra tkinter `Select Input Source` → sursa se dă din linia de comandă
- toate `cv2.imshow` / `waitKey` → stream MJPEG
- `gl_sphere` / OpenGL

**Optimizări (astea contează pe ARM):**

| Ce | Înainte | Acum |
|---|---|---|
| `get_darkest_area` | 4 bucle Python peste tot frame-ul | un `cv2.blur` + `minMaxLoc` |
| `optimize_contours_by_angle` | buclă Python per punct | vectorizat NumPy |
| praguri + dilatare + contururi | pe frame-ul întreg (640×480) | doar pe pătratul ROI din jurul pupilei |
| constante (arie minimă, mască, grosimi) | fixe pentru 640×480 | scalate cu rezoluția de procesare |
| `gaze_vector.txt` | rescris la fiecare frame | opțional, max 30 Hz (SD card) |
| encodare JPEG | — | doar când un browser chiar se uită |

**Buguri din original reparate:**

1. `get_darkest_area` aduna pixeli `uint8` într-un acumulator care rămânea
   `uint8` — pe NumPy 2 suma se învârte peste 255 și punctul „cel mai
   întunecat" iese aiurea. Se vede clar: pe același clip, originalul prinde
   pupila în 108/120 frame-uri și de multe ori se ancorează în colțul imaginii,
   portul prinde 120/120 fix pe pupilă. (Pe NumPy 1.x originalul e ok — de-asta
   funcționează la autor.)
2. `len(final_contours[0] > 5)` — compară un array cu 5, apoi ia lungimea
   array-ului boolean, deci verificarea „am cel puțin 5 puncte" nu verifica
   nimic; `fitEllipse` crapă sub 5 puncte.
3. Centrul modelului folosea `x == 320` drept „nu am date", ceea ce se declanșa
   greșit când centrul chiar era la 320. Acum e `None` explicit.
4. `mask_outside_square` desena masca cu `x + size` în loc de `bottom_right_x`
   (în varianta Pi din repo).

**Verificare că portul e fidel** — pe același clip, la aceeași rezoluție,
comparat cu originalul cu bugul de overflow reparat:

```
centrul pupilei : 120 frame-uri, diferență medie 0.21 px, max 1.47 px
```

Adică e același detector, doar mai rapid.

---

## 7. Performanță

Măsurat cu `tools/bench.py` pe `upstream/eye_test.mp4`, **pe desktop** (nu pe Pi
— numerele de pe Pi rulează-le tu, comanda e mai jos):

| Config | ms/frame | detecție |
|---|---|---|
| original 640×480 | 19.2 | 100% (108/120 cu bugul de overflow) |
| port 640×480 | 1.4 | 100% |
| port 640×480 → 320×240 | 1.1 | 100% |
| port 320×240 nativ (preset `lite`) | 0.9 | 100% |

Adică ~13× mai rapid la aceeași rezoluție. Pe Pi 3A+ raportul ar trebui să
se păstreze, dar rulează tu benchmark-ul înainte să alegi presetul:

```bash
python3 tools/bench.py upstream/eye_test.mp4 --frames 150
```

Dacă vrei să testezi tot lanțul fără camere (merge și pe laptop):

```bash
python3 -m glaze --eye upstream/eye_test.mp4 --scene "" --port 8000
```

---

## 8. WiFi de rezervă (fără rețea = access point propriu)

Dacă Pi-ul nu găsește nicio rețea cunoscută (mutat la altă locație, WiFi nou),
un serviciu separat îi pune singur radioul în mod access point și servește o
pagină de configurare, complet independent de `glaze` (rulează chiar dacă
tracker-ul e oprit/stricat).

```bash
sudo cp wifi-portal.service /etc/systemd/system/
sudo nano /etc/systemd/system/wifi-portal.service   # verifică WorkingDirectory=
sudo systemctl enable --now wifi-portal
```

Cum funcționează: după 3 verificări eșuate la rând (~30s), Pi-ul pornește o
rețea proprie `Glaze-Setup` (parolă `glaze1234` — schimb-o în
`wifi-portal.service`, la `--ap-password`). Te conectezi la ea de pe telefon
sau laptop, deschizi `http://10.42.0.1/`, pui SSID-ul și parola rețelei tale,
apasă Conectează. Pi-ul încearcă, iar dacă merge, AP-ul dispare și pagina de
setup nu mai e accesibilă — normal, Pi-ul s-a mutat pe rețeaua nouă.

Dacă rețeaua veche revine în rază cât Pi-ul e în mod AP, serviciul verifică
automat la fiecare 2 minute (coboară AP-ul câteva secunde, testează, îl repune
dacă nu găsește nimic).

```bash
python3 tools/wifi_portal.py --help   # toate opțiunile (SSID, parolă, timing)
journalctl -u wifi-portal -f          # ce face acum
```

---

## 9. Voce + identificare vorbită a obiectelor

`identify_object` (din gesturi sau din pagina `/gestures`) poate **rosti**
numele obiectului găsit, prin orice boxă USB cu ieșire jack, folosind
`espeak-ng` (instalat de `install_pi.sh`).

```bash
python3 tools/list_audio.py   # ce card ALSA are boxa ta
```

Activezi din `/settings` → secțiunea "Voce": bifează, alege vocea (`ro` sau
`en`, orice cod suportat de espeak-ng), testează cu butonul dedicat. Dacă ai
mai multe dispozitive audio USB, pune manual `plughw:CARD=N,DEV=0` (numărul
exact din `list_audio.py`) — altfel se auto-detectează primul găsit.

Limba răspunsului de la modelul de viziune (`vision_language`, tot din
`/settings`) trebuie să fie aceeași cu vocea — altfel iese text românesc citit
cu voce engleză sau invers, ininteligibil.

### Despre alimentare cu boxa USB adăugată

O boxă USB-audio consumă puțin (~50-100mA), mult sub o cameră. Problema nu e
boxa în sine, ci **dacă hub-ul e alimentat separat sau trage tot din portul
Pi-ului**. Pi 3A+ are un singur port USB; cu 2 camere deja pe hub, verifică
după ce bagi și boxa:

```bash
vcgencmd get_throttled
```

`throttled=0x0` = totul e ok. Orice altă valoare = subalimentare — fie iei un
**hub alimentat** (cu adaptor propriu la priză), fie treci camera de scenă pe
alt port dacă ai, fie folosești o sursă mai puternică pentru Pi (5V/3A oficial
Raspberry).

---

## 10. Conversația pe 4 piloni (sistemul AAC)

Sistemul complet: din ce te uiți, construiește o propoziție și o rostește tare.
Se bazează pe ideea că aproape orice mesaj se reduce la patru piloni —
**persoană, acțiune, obiect, emoție** — iar ce nu e sigur se confirmă cu
întrebări binare în cască.

### Fluxul

```
triplu-clipit
   ↓
CAPTURING   fixezi privirea ~1s pe ceva → poză cu cercul tău de privire
            (max 3 poze, sau până expiră fereastra)
   ↓
ANALYZING   UN SINGUR apel AI cu toate pozele → cei 4 piloni + încredere
   ↓
ASKING      fiecare pilon sub prag → opțiunile lui, pe rând, în cască
            privire SUS ținută 500ms = DA · JOS = NU · fără răspuns = NU
   ↓
SPEAKING    propoziția finală, pe difuzorul mare
```

Emoția **nu se întreabă niciodată** — e dedusă de model, cum ai cerut.
Pilonii se întreabă în ordinea persoană → acțiune → obiect.

### Maxim două apeluri AI

Primul apel cere și `propozitie_probabila` pentru combinația cea mai probabilă.
Dacă tot ce ai confirmat coincide cu ea, **nu se mai face al doilea apel** —
propoziția e deja acolo. Al doilea apel (compunerea frazei) pleacă doar dacă
ai schimbat ceva prin răspunsuri.

Verificat pe simulare: toți pilonii siguri → 1 apel; un pilon corectat prin
răspuns → 2 apeluri.

### Meniurile de nevoi și dureri — fără cameră, fără internet

Privire lungă (implicit 1.2s) în **stânga** → nevoi fiziologice
(sete, foame, somn, toaletă). În **dreapta** → dureri (cap, burtă, spate,
frig, cald).

Astea nu ating nici camera, nici modelul — sunt liste fixe cu fraze scrise în
[`glaze/phrases.py`](glaze/phrases.py). Intenționat: exact când e ceva urgent
sau nu e nimic relevant în cadru, sistemul trebuie să meargă și cu WiFi-ul
căzut. Dacă rețeaua pică în mijlocul unei conversații normale, propoziția se
construiește tot local, dintr-un șablon, în loc ca dispozitivul să amuțească.

### Ce vezi și ce auzi în timp real

Nu trebuie să te uiți în loguri ca să înțelegi ce face. În dashboard apare un
**banner mare, colorat**, cu pasul curent:

| Pas | Ce scrie mare | Culoare |
|---|---|---|
| adun poze | „uită-te fix la un obiect" + câte poze am | albastru |
| mă gândesc | „trimit pozele..." | galben |
| răspunde | **întrebarea, cu literă foarte mare** + „SUS = da, JOS = nu" | verde |
| rostesc | propoziția finală | roz |

Și fiecare pas are **sunetul lui distinct** în cască, ca să te poți orienta
fără să te uiți la ecran — ceea ce e chiar scopul, pentru cineva care nu poate
întoarce capul spre un monitor:

| Sunet | Când |
|---|---|
| două note urcătoare | a pornit sesiunea (te-a văzut clipind de 3 ori) |
| bip scurt înalt | a prins o poză |
| două note joase | a trimis la AI |
| bip scurt | urmează o întrebare |
| două note urcătoare | ți-a înregistrat DA |
| două note coborâtoare | ți-a înregistrat NU |
| trei note urcătoare | urmează propoziția finală |
| două note joase, lungi | ceva n-a mers |

Le oprești din `/settings` → "sunete de feedback", și ai un buton de test.

### Meniurile nu se declanșează până nu e gata modelul

Semnalul `gaze_normalized` se măsoară față de centrul sferei ochiului. Cât
timp modelul nu a convers, se măsoară față de un centru implicit cu rază zero
— deci stă blocat pe un offset constant, care arată exact ca o privire ținută
lung într-o parte, și **deschidea meniul de dureri la nesfârșit**.

Acum meniurile sunt blocate până când `sphere radius` nu mai e 0 și s-au
strâns destule raze (implicit 25). Banner-ul îți spune explicit când e cazul:
*„modelul ochiului nu e gata — uită-te în jur câteva secunde"*.

### Log-ul (`/log`)

Tot ce se întâmplă în spate, live: poziția ochiului, fiecare poză prinsă, ce
s-a trimis la model, ce a răspuns cu tot cu încrederi, fiecare întrebare pusă
și fiecare DA/NU cu câte milisecunde ai ținut privirea. Filtrezi pe categorii
(privirea e cea mai zgomotoasă, o poți ascunde).

Când propoziția iese greșit, aici vezi **unde** s-a rupt: s-a pierdut pupila,
a ghicit modelul prost, sau un DA a fost citit ca NU?

### Reglaje

Toate în `/settings` → "Conversație": timpii de fixare, câte poze, dwell-ul
pentru DA/NU, timeout-ul, pragul de încredere de la care întreabă, câte
opțiuni per pilon, dwell-ul pentru meniuri.

Cască separată de difuzor: pune `tts_question_device` (întrebările, doar
pentru tine) diferit de `tts_audio_device` (propoziția finală, pentru cameră).
Cu o singură boxă, lasă-l gol și merg amândouă pe același dispozitiv.

---

## 11. Probleme frecvente

**„could not open USB camera index 0"** — vezi ce index are:
`python3 tools/list_cameras.py`. Camera CSI ocupă și ea `/dev/video0` uneori,
deci camera USB poate fi `usb:1`.

**Imagine verde / stricată de la camera USB** — modulul nu suportă MJPG. Pune
în config `"eye_fourcc": "YUYV"` (sau `""` ca să lași driverul să decidă).

**FPS mic și `frame time` mare** — coboară la `--preset lite`, dă `--no-overlay`,
sau lasă `stream fps` pe 5–8 din interfață. Encodarea JPEG e a doua ca preț
după detecție.

**Pupila nu e găsită deloc** — camera trebuie să vadă ochiul aproape pe tot
cadrul, iar pupila să fie clar cea mai întunecată zonă. Verifică
în stream-ul de ochi și îndoaie suportul până e centrată. Dacă imaginea e
răsturnată, `flip V` / `flip H` / rotația sunt în interfață.

**`sphere radius` rămâne 0** — nu s-au strâns destule raze. Mișcă ochiul în
toate direcțiile; `rays` trebuie să crească spre 100.

**Pi-ul rămâne fără RAM** — `--preset lite`, dezactivează camera de scenă
(`--scene ""`), și limita `MemoryMax=350M` din `glaze.service` te protejează.

---

## 12. Structura proiectului

```
glaze/
  config.py         configurare + CLI
  tracker_core.py   detectorul portat (algoritmul lui Orlosky)
  cameras.py        USB/V4L2, CSI/picamera2, fișier video — fiecare pe threadul lui
  calibration.py    mapare privire → ecran/scenă (least squares) + netezire
  gestures.py       privire+clipit → simboluri → tipare → acțiuni
  conversation.py   mașina de stări AAC: poze → piloni → întrebări → propoziție
  phrases.py        meniuri nevoi/dureri + șabloane locale (merg fără internet)
  eventlog.py       jurnalul intern citit de pagina /log
  vision.py         poză + gaze point → Gemini → obiecte cu probabilități
  speech.py         text-to-speech prin espeak-ng, pe un dispozitiv audio anume
  webserver.py      server HTTP din stdlib: MJPEG + SSE + API
  app.py            legătura dintre camere, tracker, gesturi, viziune și web
static/index.html    dashboard
static/settings.html toți parametrii, live, plus poweroff/reboot
static/gestures.html construiește tipare de gesturi + „la ce mă uit?"
static/log.html      tot ce se întâmplă în spate, live
tools/bench.py        benchmark pe fișier video
tools/list_cameras.py
tools/list_audio.py   ce carduri ALSA vede Pi-ul
tools/wifi_portal.py  access point de rezervă + pagină de configurare WiFi
upstream/              codul original, ca referință
```

---

## Credit

Algoritmul de tracking și matematica modelului 3D:
**[Jason Orlosky, PhD](https://www.jeoresearch.com/aboutme.html)** —
[JEOresearch/EyeTracker](https://github.com/JEOresearch/EyeTracker)
([canal YouTube](https://www.youtube.com/@jeoresearch)).
Licența originală e în `upstream/`. Acest repo e o adaptare pentru Raspberry Pi.
