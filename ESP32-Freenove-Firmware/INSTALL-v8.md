# XiaoZhi Soeverein firmware update v8

Deze cumulatieve update bevat de eerdere v6/v7-wijzigingen plus:

- de statusoverlay heeft exact dezelfde gereserveerde hoogte als de bovenbalk;
- `Luisteren`, `Spreken` en de klok kunnen daardoor niet meer buiten de bovenbalk
  over de chattekst tekenen;
- een losse VAD-puls annuleert het follow-upvenster niet meer;
- na een antwoord sluit de sessie na 20 seconden stilte;
- alleen wanneer op de deadline werkelijk spraak actief is, volgt één eenmalige
  marge van 5 seconden; de sessie kan dus niet onbeperkt door ruis openblijven;
- de datum staat nog één teken verder naar rechts (`-20`); de tijd blijft op `-52`;
- assistentreacties blijven defensief begrensd tot drie zichtbare regels.

## Gateway: korte antwoorden

Zorg dat onder `services.gateway.environment` in `compose.yaml` staat:

```yaml
      OPENAI_INSTRUCTIONS: >-
        Antwoord in het Nederlands, tenzij de gebruiker een andere taal gebruikt.
        Beperk ieder antwoord tot maximaal drie korte zinnen en circa 35 woorden.
        Geef direct antwoord en vraag niet standaard of de gebruiker nog meer wil weten.
        Doe geen alsof-beloftes over acties die je niet kunt uitvoeren.
```

Controleer en herstart de gateway wanneer dit nog niet is toegepast:

```sh
cd /volume1/docker/xiaozhi-gateway
sudo docker compose config -q
sudo docker compose up -d --build gateway
```

## Firmware installeren en bouwen

Maak eerst een bronbackup of Git-commit. Pak daarna het archief uit over de
bestaande bronboom:

```sh
cd /volume1/docker/xiaozhi-firmware/source
tar -xzf /pad/naar/xiaozhi-soeverein-source-v8.tgz --no-same-owner --no-same-permissions
```

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

## Functionele controle

1. Controleer dat datum en tijd vrij van de molenwiek staan.
2. Start met BOOT of de externe actieve-lage GPIO2-knop een gesprek.
3. Stel twee vragen en controleer bij beide dat de status uitsluitend binnen de
   bovenbalk staat.
4. Laat na het antwoord 30 seconden stilte vallen.
5. Verwacht in de seriële log:

```text
Follow-up window armed for 20 seconds
Follow-up window expired; closing conversation session
```

6. Bij spreken precies rond de deadline mag eenmaal verschijnen:

```text
Follow-up deadline reached during speech; granting 5-second grace
```

7. De gateway hoort vervolgens `device_disconnected` te loggen. Zonder een
   nieuwe druk op BOOT of GPIO2 mag geen nieuwe sessie ontstaan.

