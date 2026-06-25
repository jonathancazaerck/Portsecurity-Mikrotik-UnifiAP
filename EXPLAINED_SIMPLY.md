# Simple Explanation — What does this program do?

## The most important thing in one sentence

This program makes sure that only real Wi-Fi devices (access points) are
allowed on certain ports of the network switch — and automatically blocks
everything else.

---

## What is a switch, what is an access point?

Think of a **switch** as a power strip for the network: many devices can be
plugged in at the same time and talk to each other.

An **access point (AP)** is the device that provides Wi-Fi in your home or
office — the small box on the wall or ceiling that you connect to the internet
through.

---

## The problem without this program

Every network device has its own "ID number", called a **MAC address**. This
number is normally unique to each device.

The problem: a clever attacker can configure their laptop to **pretend to be
a real access point** by copying its ID number (MAC spoofing). This lets them
pose as a trusted device and sneak into the network.

---

## What this program does about it

The program works like a **bouncer at a club**:

Every switch port (every "socket") starts in a **locked state**
(onboarding VLAN). A device plugged in there gets no access to the real
network at first.

Then the program checks **every 5 seconds**, looking at several things at once:

1. **Is the device actually plugged in and does it have a network connection?**
   *(Link status)*

2. **Does the UniFi controller recognise this device?**
   UniFi is the management software for all access points. Only access points
   registered there ("known MACs") are considered at all.

3. **Is the device currently active and connected to the controller?**
   The controller knows whether an access point is "online". An attacker who
   only copied the ID number would need to simultaneously take over the real
   access point — which is practically impossible.

4. **Is the device drawing power through the network cable (PoE)?**
   Real access points often get their electricity directly from the cable
   (Power over Ethernet). A regular laptop or cheap attacker device does not
   do this — and gives itself away.

Only when **all four points** check out does the program open the "door": the
port is switched to **trunk mode** and the access point gets full access to
the network.

---

## What happens when the access point is unplugged?

As soon as any one of the four points is no longer true, the program locks
the port **immediately** (back to the locked state). Any attacker who plugs
in afterwards has no chance.

---

## What happens if the internet connection or the switch drops briefly?

The program **never stops running**. It simply waits and tries again next
time. The log will show:

```
WARNING: sw01: connection lost
INFO:    sw01: connection restored
```

---

## What do I see when I look at the log?

Every 5 seconds the program writes one line per port:

```
INFO sw01/ether9  mode=trunk  ap=a8:9c:6c:da:2f:52  link=up  connected=yes  poe=powered-on
```

This means:
- **sw01/ether9** — switch 1, port 9
- **mode=trunk** — port is open (access point has access)
- **ap=a8:9c:6c:da:2f:52** — this is the "ID number" of the access point
- **link=up** — cable is plugged in
- **connected=yes** — UniFi confirms: this access point is online
- **poe=powered-on** — device is drawing power from the cable

If `onboarding` appears instead of `trunk`, the port is locked.

---

## Quick summary

| Situation | What happens |
|---|---|
| Real AP plugged in | Port opens after a brief check |
| AP unplugged or offline | Port locks immediately |
| Unknown device with copied MAC | Port stays locked (no PoE, not active in UniFi) |
| Switch or UniFi briefly offline | Program waits and continues afterwards |
| Program restarts | Everything is checked immediately, no data loss |
