# assets/

Ide kerül a program ikonja az EXE-buildhez:

- **`tradeforge.ico`** — Windows ikon (ajánlott méretek egy .ico-ban: 16, 32, 48, 256 px).

Ha a fájl megvan, a PyInstaller build (`build/TradeForge.spec`) automatikusan
felhasználja. Ha hiányzik, az EXE ikon nélkül épül.

PNG-ből .ico-t pl. online konverterrel vagy Pillow-val készíthetsz:

```python
from PIL import Image
Image.open("tradeforge.png").save(
    "tradeforge.ico", sizes=[(16,16),(32,32),(48,48),(256,256)])
```
