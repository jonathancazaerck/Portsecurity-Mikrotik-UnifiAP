# Eenvoudige uitleg — Wat doet dit programma?

## Het belangrijkste in één zin

Dit programma zorgt ervoor dat alleen echte wifi-apparaten (access points)
worden toegelaten op bepaalde poorten van de netwerkswitch — en blokkeert
alle andere apparaten automatisch.

---

## Wat is een switch, wat is een access point?

Stel je een **switch** voor als een stekkerdoos voor het netwerk: veel apparaten
kunnen tegelijk worden aangesloten en met elkaar communiceren.

Een **access point (AP)** is het apparaat dat wifi levert in je huis of kantoor
— het kleine kastje aan de muur of het plafond waarmee je verbinding maakt
met het internet.

---

## Het probleem zonder dit programma

Elk netwerkapparaat heeft een eigen "identiteitsnummer", het zogenaamde
**MAC-adres**. Dit nummer is normaal gesproken uniek.

Het probleem: een slimme aanvaller kan zijn laptop zo instellen dat hij
**het nummer van een echt access point nabootst** (MAC-spoofing). Zo kan hij
doen alsof hij een vertrouwd apparaat is en toegang krijgen tot het netwerk.

---

## Wat dit programma daartegen doet

Het programma werkt als een **uitsmijter bij een club**:

Elke switchpoort (elke "stopcontact") begint in een **geblokkeerde stand**
(onboarding-VLAN). Een apparaat dat daar wordt aangesloten krijgt voorlopig
geen toegang tot het echte netwerk.

Vervolgens controleert het programma **elke 5 seconden** meerdere dingen
tegelijk:

1. **Is het apparaat überhaupt aangesloten en heeft het een netwerkverbinding?**
   *(Linkstatus)*

2. **Kent de UniFi-controller dit apparaat?**
   UniFi is het beheerprogramma voor alle access points. Alleen access points
   die daarin geregistreerd zijn ("bekende MAC's") komen überhaupt in
   aanmerking.

3. **Is het apparaat op dit moment actief verbonden met de controller?**
   De controller weet of een access point "online" is. Een aanvaller die
   alleen het nummer heeft gekopieerd, zou tegelijkertijd het echte access
   point moeten overnemen — dat is vrijwel onmogelijk.

4. **Trekt het apparaat stroom via de netwerkkabel (PoE)?**
   Echte access points halen vaak hun stroom rechtstreeks uit de kabel (Power
   over Ethernet). Een gewone laptop of goedkoop aanvallersapparaat doet dat
   niet — en verraadt zich daarmee.

Alleen als **alle vier punten** kloppen, opent het programma de "deur": de
poort wordt omgeschakeld naar **trunk-modus** en het access point krijgt
volledige toegang tot het netwerk.

---

## Wat gebeurt er als het access point wordt losgekoppeld?

Zodra één van de vier punten niet meer klopt, blokkeert het programma de
poort **meteen weer** (terug naar de geblokkeerde stand). De aanvaller die
daarna aansluit heeft geen kans.

---

## Wat gebeurt er als de internetverbinding of de switch even uitvalt?

Het programma **stopt nooit**. Het wacht gewoon en probeert het de volgende
keer opnieuw. In het logboek staat dan:

```
WAARSCHUWING: sw01: verbinding verbroken
INFO:         sw01: verbinding hersteld
```

---

## Wat zie ik als ik in het logboek kijk?

Elke 5 seconden schrijft het programma één regel per poort:

```
INFO sw01/ether9  mode=trunk  ap=a8:9c:6c:da:2f:52  link=up  connected=yes  poe=powered-on
```

Dit betekent:
- **sw01/ether9** — switch 1, poort 9
- **mode=trunk** — poort is open (access point heeft toegang)
- **ap=a8:9c:6c:da:2f:52** — dit is het "identiteitsnummer" van het access point
- **link=up** — kabel is aangesloten
- **connected=yes** — UniFi bevestigt: dit access point is online
- **poe=powered-on** — apparaat trekt stroom uit de kabel

Als er `onboarding` staat in plaats van `trunk`, is de poort geblokkeerd.

---

## Kort samengevat

| Situatie | Wat gebeurt er |
|---|---|
| Echte AP aangesloten | Poort opent zich na korte controle |
| AP losgekoppeld of uitgevallen | Poort blokkeert meteen |
| Vreemd apparaat met gekopieerd MAC | Poort blijft geblokkeerd (geen PoE, niet actief in UniFi) |
| Switch of UniFi even offline | Programma wacht en gaat daarna verder |
| Programma herstart | Alles wordt meteen opnieuw gecontroleerd, geen gegevensverlies |
