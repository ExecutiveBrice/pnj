# La Compagnie des Huit Catastrophes
## Le Médaillon de l'Omnipotence Approximative

Campagne française adaptée du livret fourni : **32 PNJ locaux, Archibald,
huit scénarios au choix et un final commun**. Chaque PNJ a sa personnalité,
ses connaissances, ses réactions et son typage de langage. Les fiches gardent
les identifiants du livret et les conditions des petites confidences.

## Ouvrir dans NPC

Depuis la racine du projet, double-cliquer sur
`Lancer les Huit Catastrophes.bat`, ou lancer :

```powershell
uv run --python 3.12 --locked --extra pocket npc gui CompagnieDesHuitCatastrophes
```

On peut aussi ouvrir ce dossier avec « Parcourir… » dans l'interface existante.
La configuration reprend Ollama `qwen2.5:7b-instruct`, Whisper français et
Pocket TTS français. Les modèles et services doivent être disponibles comme
pour la campagne existante. Aucun service ni téléchargement n'a été lancé
pendant la création de ces fichiers. La lecture audio locale est activée.

Archibald est sélectionné en premier grâce au fichier racine `character.md`.
En console, `/npc` liste les personnages ; `/npc grum`, `/npc gertrude` ou
`/npc character` sélectionne un interlocuteur. Dans l'interface, utiliser la
liste des personnages. [VOIX.md](VOIX.md) donne tous les noms de sélection.

## Avant la partie

Lire [AMBIANCE.md](AMBIANCE.md), [la progression](mj/PROGRESSION.md) et
[les états de scène](mj/ETATS_DE_SCENE.md). Choisir librement trois zones avec
le groupe, puis préparer les trois supports sur votre propre carte.
Les scénarios détaillés sont dans [mj/scenarios](mj/scenarios) ; les objets,
le bestiaire, les indices et le final sont également dans `mj/`.

Le livret original est conservé intégralement dans `mj/livret-source.md`.
La direction sensorielle et le détail des registres de langue complètent le
livret pour cette adaptation. Les profils CO Fantasy 2 sont les profils maison
du document, sans nouvelle validation de leur équilibrage en partie.

## Ce que reçoit le modèle

- `adventure.md` : cadre et ambiance publics communs.
- `character.md` ou `characters/<nom>.md` : un seul personnage, ses petites
  confidences conditionnelles, son registre et ses marchandises éventuelles.
- `lore/mission.md` pour Archibald ; `lore/<nom>/scene.md` pour chaque PNJ local :
  seulement le décor observable de sa scène, sans coulisses des autres PNJ.
- `secrets.md` : vrai souhait d'Archibald, corps masqué jusqu'à l'accord du MJ.

`mj/`, `README.md`, `AMBIANCE.md` et `VOIX.md` ne sont pas injectés dans les
prompts. Ne pas ajouter le livret entier au lore : il contient les solutions.
Les petites confidences des PNJ restent dans leurs fiches pour que leurs
conditions de dialogue fonctionnent sans bloquer les renseignements utiles.

Les mémoires sont séparées. Le MJ doit transmettre les accords communs aux
PNJ concernés et choisir le prochain indice. Après la perte de parole d'une
créature, sélectionner un autre personnage et reprendre la narration au MJ.
L'outil ne lance ni progression automatique ni bande-son. Les voix choisies
offrent des timbres distincts ; hauteur, débit et tics sont des directions de
jeu, et non des réglages audio garantis.

## Parcours des scénarios

| Zone | Scénario | Fiche MJ |
| --- | --- | --- |
| ville | Retrait refusé, troll énervé | [Ouvrir](mj/scenarios/ville.md) |
| village | La révolte des poules de Tilverton | [Ouvrir](mj/scenarios/village.md) |
| foret | Le chêne a les glands | [Ouvrir](mj/scenarios/foret.md) |
| marais | La princesse exige un remboursement | [Ouvrir](mj/scenarios/marais.md) |
| montagne | Atchoum, vous êtes cuits | [Ouvrir](mj/scenarios/montagne.md) |
| plaine | La grève des hommes de paille | [Ouvrir](mj/scenarios/plaine.md) |
| desert | Le génie du forfait illimité | [Ouvrir](mj/scenarios/desert.md) |
| plage | Le trésor passe à la pince | [Ouvrir](mj/scenarios/plage.md) |
