# HEMS Dispatch : génération des régions dans le cloud

Ce dossier génère les fichiers de région (LZ, héliports d'hôpitaux, lieux d'intervention, bases) pour le monde entier, sur les serveurs de GitHub, sans rien télécharger sur ton PC. Les fichiers sont publiés dans la release **regions** d'un dépôt GitHub public, avec `index.json`, la liste que l'app télécharge.

## Contenu du dépôt public

```
.github/workflows/build-regions.yml   le travail GitHub Actions
pipeline/plan.py                      choix des régions (un pays, ou ses sous-régions s'il est grand)
pipeline/build_chunk.py               téléchargement OSM, calcul, compression, mise en ligne
pipeline/merge_index.py               index.json
tools/osm-builder/                    le même générateur que dans le projet (copie)
```

Le dépôt doit être **public** : les minutes GitHub Actions sont alors gratuites et illimitées, et les clients peuvent télécharger les fichiers sans compte. Le code de l'app ne va pas dans ce dépôt, seulement le générateur.

## Lancer une génération

GitHub › le dépôt › **Actions** › **Build regions** › **Run workflow**, puis dans « regions » :

- `switzerland` : un seul pays (premier essai conseillé)
- `switzerland,austria,bayern` : plusieurs pays ou sous-régions
- `europe` : tout un continent
- `all` : le monde entier (plusieurs heures)

Une génération complète repart aussi toute seule tous les trois mois (1er janvier, avril, juillet, octobre), pour suivre les mises à jour d'OpenStreetMap.

## Découpage

Un fichier par pays. Un pays dont l'extrait OpenStreetMap dépasse 900 Mo est découpé en ses sous-régions Geofabrik (États des USA, provinces du Canada, Länder allemands, régions françaises, etc.), pour garder des fichiers légers et une génération qui tient dans la mémoire des serveurs GitHub. Le seuil se change dans « split_mb ».

Les régions sans aucun héliport d'hôpital ou sans lieu d'intervention dans OpenStreetMap ne sont pas publiées : l'app n'y trouverait aucune mission.

## Dans l'app

L'adresse de la liste est dans `data/regions-source.json` du projet :

```
https://github.com/heliromandy/hems-dispatch-regions/releases/download/regions/index.json
```

## Données

Données OpenStreetMap, licence ODbL 1.0, téléchargées chez Geofabrik (download.geofabrik.de). L'attribution est dans chaque fichier et dans `index.json`, et l'app l'affiche.
