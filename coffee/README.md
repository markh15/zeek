# Brazil coffee: top municipalities → cadastral fazendas → OSM coffee farms

`brazil_coffee_fazendas.ipynb`:

1. Ranks every Brazilian municipality by coffee production, using IBGE PAM table 1613 (total, arábica and canéfora, averaged over the last 3 years). Outputs a top-15 chart, a trend chart and a national choropleth.
2. Picks the #1 municipality and downloads its **cadastral rural properties**, i.e. the CAR / SICAR polygons from the public WFS. Instead of the WFS you can point it at a local CAR or INCRA SIGEF shapefile.
3. Queries **OpenStreetMap** (Overpass) for coffee fields and farms, overlays them on the properties, and flags the **coffee fazendas**.
4. Draws static maps on an OSM basemap (properties by size class, coffee fazendas, a close-up) and an interactive Folium map with OSM, CARTO and satellite layers. Exports CSV, GeoPackage, HTML and PNG files to `outputs/`.

```bash
pip install -r requirements.txt
jupyter lab brazil_coffee_fazendas.ipynb
```

The notebook needs internet access to `servicodados.ibge.gov.br`, `geoserver.car.gov.br`, an Overpass mirror, and OSM, CARTO and Esri tile servers. It caches every response under `cache/`. The settings are in the first code cell: `TOP_N`, `AVG_YEARS`, `MUNICIPALITY_OVERRIDE`, `CADASTRE_SOURCE` / `CADASTRE_LOCAL_PATH` and `MIN_COFFEE_HA`.

**Limits:** CAR boundaries are self-declared and can overlap. OSM covers coffee only partially, and the notebook prints how much of IBGE's harvested area OSM covers. For wall-to-wall coverage, use a remote-sensing coffee mask such as MapBiomas.
