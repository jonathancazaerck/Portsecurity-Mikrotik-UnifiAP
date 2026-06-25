# Einfache Erklärung — Was macht dieses Programm?

## Das Wichtigste in einem Satz

Dieses Programm passt auf, dass nur echte WLAN-Geräte (Access Points) an
bestimmten Steckdosen des Netzwerk-Switches zugelassen werden — und sperrt
alle anderen automatisch aus.

---

## Was ist ein Switch, was ist ein Access Point?

Stell dir einen **Switch** wie eine Steckerleiste für das Netzwerk vor: viele
Geräte können gleichzeitig eingesteckt werden und miteinander reden.

Ein **Access Point (AP)** ist das Gerät, das das WLAN in deiner Wohnung
bereitstellt — das kleine Kästchen an der Wand oder der Decke, über das du
dich mit dem Internet verbindest.

---

## Das Problem ohne dieses Programm

Jedes Netzwerkgerät hat eine eigene "Personalausweis-Nummer", die sogenannte
**MAC-Adresse**. Diese Nummer ist normalerweise einmalig.

Das Problem: Ein schlauer Angreifer kann seinen Laptop so einstellen, dass er
**die Nummer eines echten Access Points nachahmt** (MAC-Spoofing). Damit
könnte er sich als vertrauenswürdiges Gerät ausgeben und ins Netzwerk kommen.

---

## Was dieses Programm dagegen tut

Das Programm funktioniert wie ein **Türsteher an einem Club**:

Jeder Switch-Port (jede "Steckdose") beginnt in einem **gesperrten Zustand**
(Onboarding-VLAN). Ein Gerät, das dort eingesteckt wird, bekommt zunächst
keinen Zugang zum eigentlichen Netzwerk.

Dann prüft das Programm **alle 5 Sekunden** mehrere Dinge gleichzeitig:

1. **Ist das Gerät überhaupt eingesteckt und hat es eine Netzwerkverbindung?**
   *(Link-Status)*

2. **Kennt der UniFi-Controller dieses Gerät?**
   UniFi ist das Verwaltungsprogramm für alle Access Points. Nur Access Points,
   die dort registriert sind ("bekannte MACs"), kommen überhaupt in Frage.

3. **Ist das Gerät gerade aktiv mit dem Controller verbunden?**
   Der Controller weiß, ob ein Access Point gerade "online" ist. Ein Angreifer,
   der nur die Nummer kopiert hat, müsste gleichzeitig den echten Access Point
   übernehmen — das ist praktisch unmöglich.

4. **Zieht das Gerät Strom über das Netzwerkkabel (PoE)?**
   Echte Access Points beziehen oft ihren Strom direkt aus dem Kabel (Power
   over Ethernet). Ein normaler Laptop oder ein billiges Angreifer-Gerät tut
   das nicht — und verrät sich damit.

Nur wenn **alle vier Punkte** stimmen, öffnet das Programm die "Tür":
der Port wird auf **Trunk-Modus** geschaltet und der Access Point bekommt
vollen Zugang zum Netzwerk.

---

## Was passiert wenn der Access Point abgesteckt wird?

Sobald einer der vier Punkte nicht mehr erfüllt ist, sperrt das Programm den
Port **sofort wieder** (zurück in den gesperrten Onboarding-Zustand). Der
Angreifer, der danach einsteckt, hat keine Chance.

---

## Was passiert wenn die Internetverbindung oder der Switch kurz ausfällt?

Das Programm hört **niemals auf zu laufen**. Es wartet einfach und versucht
es beim nächsten Mal wieder. Im Log (Protokoll) steht dann:

```
WARNUNG: sw01: Verbindung unterbrochen
INFO:    sw01: Verbindung wiederhergestellt
```

---

## Was sehe ich, wenn ich ins Protokoll schaue?

Alle 5 Sekunden schreibt das Programm eine Zeile pro Port:

```
INFO sw01/ether9  mode=trunk  ap=a8:9c:6c:da:2f:52  link=up  connected=yes  poe=powered-on
```

Das bedeutet:
- **sw01/ether9** — Switch 1, Steckdose 9
- **mode=trunk** — Port ist offen (Access Point hat Zugang)
- **ap=a8:9c:6c:da:2f:52** — das ist die "Personalausweis-Nummer" des Access Points
- **link=up** — Kabel ist eingesteckt
- **connected=yes** — UniFi bestätigt: dieser Access Point ist online
- **poe=powered-on** — Gerät zieht Strom aus dem Kabel

Wenn statt `trunk` dort `onboarding` steht, ist der Port gesperrt.

---

## Kurz zusammengefasst

| Situation | Was passiert |
|---|---|
| Echter AP eingesteckt | Port öffnet sich nach kurzer Prüfung |
| AP abgesteckt oder ausgefallen | Port sperrt sich sofort |
| Fremdes Gerät mit kopierter MAC | Port bleibt gesperrt (kein PoE, nicht in UniFi aktiv) |
| Switch oder UniFi kurz offline | Programm wartet, macht danach weiter |
| Programm startet neu | Alles wird sofort neu geprüft, kein Datenverlust |
