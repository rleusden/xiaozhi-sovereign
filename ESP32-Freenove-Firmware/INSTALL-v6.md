# XiaoZhi Soeverein firmware update v6

Deze update bouwt voort op de eerder geïnstalleerde v5 idle-screenbron.

## Wijzigingen

- tijd en datum 16 pixels naar links;
- configuratie-AP en DHCP-hostnaam gebruiken `XiaoZhi-Soeverein-XXXX`;
- na ieder antwoord blijft een follow-upvenster van 20 seconden open;
- na 20 seconden stilte sluit het bord het WebSocket-audiokanaal;
- het sluiten van de WebSocket beëindigt ook de OpenAI Realtime-sessie op de gateway;
- een begonnen vervolgvraag wordt niet na 20 seconden afgebroken;
- een nieuwe spraaksessie start alleen met de BOOT-knop of GPIO2;
- GPIO2 is actief laag en gebruikt de interne pull-up;
- korte schermaanrakingen starten geen spraaksessie meer;
- lang aanraken blijft wifi-configuratie openen.

## Externe drukknop

Sluit een potentiaalvrije momentdrukknop aan tussen:

- GPIO2
- GND

Voer geen externe spanning aan GPIO2 toe.

## Installeren op de NAS

Maak eerst een bronbackup of Git-commit. Pak daarna het archief uit over de
bestaande bronboom:

```sh
cd /volume1/docker/xiaozhi-firmware/source
tar -xzf /pad/naar/xiaozhi-soeverein-source-v6.tgz --no-same-owner --no-same-permissions
```

Verwijder de gegenereerde assets alleen wanneer het windmolenbestand of de
assetconfiguratie ook is gewijzigd. Voor deze update is dat niet nodig.

Build vervolgens op dezelfde reproduceerbare manier als v5:

```sh
sudo docker run --rm \
  -v /volume1/docker/xiaozhi-firmware:/workspace \
  -w /workspace/source \
  espressif/idf:v6.1 \
  bash -lc 'python3 scripts/build.py \
    freenove-esp32s3-display-2.8-lcd \
    --name xandria-freenove-esp32s3-display-2.8-lcd \
    --language nl-NL \
    --wake-word disabled \
    --zip'
```

## Verwachte seriële logging

Na een antwoord:

```text
Follow-up window armed for 20 seconds
```

Na twintig seconden zonder vervolgvraag:

```text
Follow-up window expired; closing conversation session
```

De gateway hoort daarna `device_disconnected` te loggen. Een volgende druk op
BOOT of GPIO2 opent een nieuwe verbinding en levert opnieuw `device_connected`.

## Functionele controle

1. Druk BOOT in en stel een vraag.
2. Stel binnen twintig seconden een vervolgvraag; de sessie moet blijven bestaan.
3. Laat na het antwoord twintig seconden stilte vallen; de gateway moet de sessie sluiten.
4. Spreek zonder een knop in te drukken; er mag geen nieuwe sessie ontstaan.
5. Druk GPIO2 kort naar GND; dit moet hetzelfde werken als BOOT.
6. Controleer dat een korte schermaanraking geen spraaksessie opent.
7. Controleer dat lang aanraken nog steeds wifi-configuratie opent.

