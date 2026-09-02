# Stock icons de personajes

Esta carpeta va vacía en el repo a propósito: los íconos ("stock icons") de
los personajes de Melee son sprites extraídos del propio juego, propiedad de
Nintendo/HAL Laboratory. Este proyecto no los redistribuye.

## Cómo conseguirlos

1. Cada ícono se busca por nombre en [SSBWiki](https://www.ssbwiki.com), en
   la página del personaje correspondiente (buscar "Head" o "Stock icon").
2. Guardalo en esta carpeta con el nombre exacto:
   `NombreDelPersonajeHeadSSBM.png`

   Ejemplos: `FoxHeadSSBM.png`, `DonkeyKongHeadSSBM.png`, `MrGameWatchHeadSSBM.png`

   El nombre se arma sacando espacios, puntos y el símbolo `&` del nombre en
   español que usa la app (ver `_char_icon_slug()` en `app.py`).

## Si no tenés los íconos

No pasa nada — la app funciona igual. Todos los lugares que muestran un
ícono de personaje (GENERAL, PERSONAJES, PROGRESO, HISTORIAL) caen a un
texto/medalla si el archivo no existe. Es un "nice to have", no una
dependencia dura.
