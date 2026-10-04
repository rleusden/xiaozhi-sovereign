# XiaoZhi Soeverein update v7

Deze update bevat v6 plus twee schermcorrecties:

- de datum wordt één teken naar rechts gecorrigeerd; de tijd blijft staan;
- assistentreacties worden op het bord defensief tot drie zichtbare regels begrensd;
- de bovenbalk is tijdens de chat ondoorzichtig, zodat tijd/status niet over tekst heen staat.

De firmwaregrens voorkomt alleen visuele overflow. Om ook uitgesproken antwoorden
kort te houden, configureer je daarnaast de gateway-instructie.

## Gateway: korte antwoorden

Voeg onder `services.gateway.environment` in `compose.yaml` toe:

```yaml
      OPENAI_INSTRUCTIONS: >-
        Antwoord in het Nederlands, tenzij de gebruiker een andere taal gebruikt.
        Beperk ieder antwoord tot maximaal drie korte zinnen en circa 35 woorden.
        Geef direct antwoord en vraag niet standaard of de gebruiker nog meer wil weten.
        Doe geen alsof-beloftes over acties die je niet kunt uitvoeren.
```

Controleer de YAML en herstart daarna alleen de gateway:

```sh
cd /volume1/docker/xiaozhi-gateway
sudo docker compose config -q
sudo docker compose up -d --build gateway
```

## Firmware installeren

Maak eerst een bronbackup of Git-commit. Pak daarna het archief uit over de
bestaande bronboom:

```sh
cd /volume1/docker/xiaozhi-firmware/source
tar -xzf /pad/naar/xiaozhi-soeverein-source-v7.tgz --no-same-owner --no-same-permissions
```

Bouw vervolgens opnieuw:

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

## Controle

1. Controleer op het rustscherm dat de tijd ongewijzigd staat en de datum één
   teken naar rechts is verplaatst.
2. Vraag om een bewust lang antwoord. De audio hoort kort te blijven door de
   gateway-instructie.
3. Als een provider toch meer tekst terugstuurt, toont het bord maximaal drie
   regels met een afbreekteken.
4. Controleer dat de tijd/status niet meer over de eerste antwoordregel staat.

