Baue einen Python-Watchdog der UniFi APs an MikroTik Switches absichert.

**Konzept:** Switch-Port ist standardmäßig im Onboarding-VLAN (99). Watchdog pollt UniFi Controller alle 10s – AP connected → Port auf Trunk, AP offline → Port zurück auf Onboarding.

Wenn Port auf Onboarding: dot1x aktiv auf dem Port
Wenn Port auf Trunk: dot1x ist deaktiviert

**Umgebung:**

* UniFi Controller: https://192.0.2.1:443
* MikroTik sw01: 192.0.2.2, RouterOS 7.x, API-SSL 8729
* Onboarding-VLAN: 99, Management-VLAN: 10 (untagged/PVID), Trunk-VLANs: 30, 50 (tagged)
* Passwörter aus Umgebungsvariablen: `WATCHDOG_UNIFI_PASSWORD`, `WATCHDOG_MIKROTIK_PASSWORD`

**RouterOS 7 Eigenheiten:** `plaintext_login=True`, `ssl_verify=False`, kein link-down-script (Netwatch stattdessen), dynamic VLAN-Einträge nicht änderbar → statische vorab anlegen.

**Multi-Switch:** Port wird dynamisch per MAC auf allen Switches gesucht. Setup-Script für initiale VLAN-Einträge und Netwatch. Unit-Tests mit gemockter RouterOS API.
