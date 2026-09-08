# Tests für das Blender-Addon

Zwei Suiten, absichtlich getrennt: eine läuft in Sekunden ohne Blender, die andere braucht
Blender und einen echten WMV-Export. Der Schnitt liegt dort, wo `_print_report()` aufhört,
die Szene anzufassen — reine Rechnung auf der einen Seite, Geometrie auf der anderen.

Der Nutzen dieser Trennung ist nicht theoretisch: als der erste echte Durchlauf abbrach,
blieb `test_print_report.py` grün und der Fehler war damit sofort in der Messhälfte lokalisiert,
nicht in der Rechnung.

## Ohne Blender — die Arithmetik

```bash
python tests/test_print_report.py
```

Lädt das Addon mit gestubbtem `bpy` und prüft Maßstab, zurückgerechnete Solidify-Dicke,
Voxelgröße, Bauraumwarnung, die Einordnung der Teile und das Verhalten bei leerer oder
höhenloser Szene. Braucht nichts außer CPython.

## Mit Blender — die Kette

```bash
blender -b --factory-startup --python tests/test_print_pipeline.py -- <charakter.fbx> [ausgabeordner]
```

Importiert einen echten Export, fährt alle sechs Schritte, liest die geschriebene STL zurück
und misst sie nach. Ohne Ausgabeordner landet die STL in einem temporären Verzeichnis.

Geprüft wird unter anderem, dass nach Schritt 2 kein Skelett und kein Modifier übrig ist, dass
das Ergebnis wasserdicht ist, dass die Zielhöhe **in der Datei** ankommt, und dass jeder Schritt
auf einer leeren Szene oder ohne Sidecar verweigert statt still nichts zu tun.

Beide Suiten enden mit Exit-Code 1, wenn etwas fehlschlägt.

## Was jeweils geprüft wird

`test_print_report.py` deckt zusätzlich zur Arithmetik ab:

- **Die zwei Prüfregeln aus DRUCK-IDEEN.md als Zusicherungen.** Profil löschen — der Bericht
  muss weiter etwas Sinnvolles liefern statt abzustürzen. Werte absurd setzen (Bauraum 10 mm,
  kleinstes Merkmal 5 mm) — es müssen sinnvolle Warnungen herauskommen, keine hart
  geschriebene Zahl weiterrechnen.
- **CIELAB gegen Lehrbuchwerte** (Weiß L\*100, mittleres Grau L\*53,6, Rot 53,24/80,09/67,20).
  Genau deshalb ist die Farbkarte in LAB und nicht in RGB: Zahlen, die etwas bedeuten, kann man
  prüfen.
- **Die Schnittsuche** an einer künstlichen Säule mit bekannter Engstelle — findet sie sie im
  Fenster, und lässt sie sich von einer Engstelle außerhalb des Fensters *nicht* wegziehen.

## Was diese Tests schon gefangen haben

- `bmesh.from_object()` wirft auf den leeren Objekten, die `_separate_by_material` pro
  flächenlosem Materialslot hinterlässt — auf echten Charakteren, nie auf einer Fixture.
- `object.dimensions` ist direkt nach `join()` noch der alte Wert, weil der Depsgraph noch
  nicht nachgezogen hat. Der Voxel-Wächter maß dadurch das falsche Objekt und griff nie.
- `bpy.ops` **wirft** `RuntimeError`, wenn ein Operator `ERROR` meldet, statt `CANCELLED`
  zurückzugeben. Der Sammelknopf starb daran mit einem Traceback, statt den Schritt zu nennen,
  der verweigert hatte — also genau an seiner einzigen Aufgabe.
- `object.dimensions` war auch beim Zerteilen wieder falsch: dieselbe Depsgraph-Verzögerung,
  diesmal meldete jedes geschnittene Teil die Höhe der ungeschnittenen Figur.
- `separate(type='LOOSE')` trennt nach **Zusammenhang**, nicht nach Scheibe. Auf einem
  vernetzten Körper mit vielen kleinen Inseln wurden daraus 107 „Teile", wo drei bestellt waren.
- `path_mode='COPY'` schreibt beim Vollfarb-Export **keine** Texturen, weil sie in der FBX
  gepackt sind und keine Datei auf der Platte haben. Das ZIP sah gültig aus und enthielt keine
  Farbe — der Test zählt deshalb die PNGs im Archiv, nicht bloß dessen Existenz.
- Ein festes Nahtsuchfenster schob beim Orc eine Scheibe über die Bauhöhe. Der Test läuft
  gegen beide Charaktere, weil genau solche Fälle zeichenabhängig sind.
