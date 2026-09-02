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
python3 -m glaze --preset lite --eye usb:0 --scene csi
```

Apoi, pe laptop: `http://<ip-ul-pi-ului>:8000/`

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

## 8. Probleme frecvente

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

## 9. Structura proiectului

```
glaze/
  config.py         configurare + CLI
  tracker_core.py   detectorul portat (algoritmul lui Orlosky)
  cameras.py        USB/V4L2, CSI/picamera2, fișier video — fiecare pe threadul lui
  calibration.py    mapare privire → ecran/scenă (least squares) + netezire
  webserver.py      server HTTP din stdlib: MJPEG + SSE + API
  app.py            legătura dintre camere, tracker și web
static/index.html   interfața
tools/bench.py      benchmark pe fișier video
tools/list_cameras.py
upstream/           codul original, ca referință
```

---

## Credit

Algoritmul de tracking și matematica modelului 3D:
**[Jason Orlosky, PhD](https://www.jeoresearch.com/aboutme.html)** —
[JEOresearch/EyeTracker](https://github.com/JEOresearch/EyeTracker)
([canal YouTube](https://www.youtube.com/@jeoresearch)).
Licența originală e în `upstream/`. Acest repo e o adaptare pentru Raspberry Pi.
