# NPC — personnages de jeu de rôle à voix haute

NPC fait parler des personnages de jeu de rôle avec des modèles locaux :
Whisper transcrit le joueur, un modèle servi par Ollama ou une API compatible
OpenAI produit la réponse, puis Piper, Pocket TTS ou Qwen3-TTS la prononce.
L'interface de bureau, la console et le compagnon mobile utilisent le même moteur.
Les modèles doivent être téléchargés avant de fonctionner hors ligne.

## Installation et lancement

Python 3.11 ou supérieur et `uv` sont nécessaires. Depuis ce dépôt :

```sh
uv sync --frozen --group dev
uv run npc init campaigns/demo
uv run npc doctor --fix campaigns/demo
uv run npc gui campaigns/demo
```

`doctor` vérifie les services locaux, les modèles, l'audio et la campagne.
Le modèle de dialogue par défaut est `qwen2.5:7b-instruct` sur
`http://localhost:11434` ; Ollama doit être démarré pour les dialogues.
Les réglages facultatifs `[llm] temperature` et `top_p` sont transmis aux deux
backends, y compris en streaming ; `top_k` et `repeat_penalty` sont réservés à
Ollama. Sans ces clés, le moteur conserve les valeurs du serveur. Pour examiner
une dérive de dialogue, commencer par `temperature = 0.2` en ne changeant qu'un
réglage à la fois. Une température faible réduit la variété, sans garantir la
justesse : `0` peut reproduire systématiquement une inversion de rôles.
`num_ctx = 8192` permet de conserver davantage de contexte sous Ollama lorsque
les fiches et l'historique le nécessitent ; augmenter cette valeur ne corrige
pas à lui seul le raisonnement et consomme davantage de mémoire.
Piper utilise par défaut les voix de `~/.local/share/npc/voices/`.
Selon le système, PortAudio et Tk doivent aussi être installés pour le son et
l'interface de bureau. La configuration complète est commentée dans
[src/npc/templates/config.toml](src/npc/templates/config.toml).

Pour utiliser Pocket ou Qwen, installer l'option correspondante puis régler
`[tts] backend` dans le `config.toml` de la campagne :

```sh
uv sync --frozen --group dev --extra pocket
# ou
uv sync --frozen --group dev --extra qwen
```

Les options des dépendances vocales dépendent de la version de Python et de la
plateforme ; leurs contraintes sont définies dans `pyproject.toml`.

En console : `uv run npc run campaigns/demo`. Le push-to-talk global utilise
evdev sous Linux ; le bouton d'enregistrement de l'interface de bureau est
l'entrée micro sur Windows et macOS. `/say Bonjour` permet de dialoguer au clavier.
Une ligne sans commande donne une consigne au MJ. `/npc` liste les personnages,
`/npc <nom ou identifiant>` en sélectionne un, `/reload` relit les fichiers,
`/save` résume la session, `/end` termine en sauvegardant et `/quit` termine sans
résumé final. Les transcriptions sont écrites au fil des échanges.

Dans l’interface de bureau, l’onglet **test** permet d’écouter les voix du
générateur sélectionné avec un texte modifiable (500 caractères maximum), puis
d’arrêter l’essai. Il propose les voix Piper installées, les voix Pocket TTS ou
les descriptions Qwen VoiceDesign configurées dans la campagne. **Actualiser**
relit les voix disponibles après une installation. Les essais se font hors
session, sans micro ni serveur de dialogue, et ne changent pas les voix des PNJ.
Le premier chargement peut nécessiter le téléchargement du modèle vocal.

L'ouverture de la session, la sélection d'un PNJ et `/reload` ne déclenchent
aucune génération de phrase ni prise de parole. Le PNJ attend une parole du
joueur ou une demande explicite du MJ via **Faire parler un PNJ**.

Dans l’interface de bureau, la zone **Faire parler un PNJ** permet de choisir
un personnage et de déclencher une **Présentation**, une réplique pour
**Meubler pour attendre** ou une **Salutation**. Sa personnalité, son attitude
et sa façon de parler proviennent de sa fiche ; aucun ton de réplique n'est
ajouté. La synthèse utilise la voix attribuée au PNJ dans la campagne.
Ces actions ne comptent pas comme un tour joueur et ne modifient pas ses
consignes permanentes. Une commande reçue pendant un échange attend son tour ;
un enregistrement en cours doit être terminé avant de déclencher cette prise
de parole.

## Campagnes

Les campagnes sont des dossiers indépendants de fichiers Markdown et TOML.
`campaigns/` est ignoré par Git pour conserver les parties hors du code source.
Ouvrir une campagne existante avec `uv run npc gui /chemin/vers/campagne`.

Trois dispositions peuvent coexister :

| Disposition | Fiches | Contexte de référence | Secrets et mémoire |
| --- | --- | --- | --- |
| Un PNJ | `character.md` | `lore/*.{md,txt,pdf}` | `secrets.md`, `logbook.md` |
| Plusieurs PNJ | `characters/<id>.md` | `lore/<id>/` | `secrets/<id>.md`, `logbooks/<id>.md` |
| Zones | `zones/<zone>/characters/<id>.md` | `zones/<zone>/lore.md` et `lore/<id>/` dans cette zone | `secrets/<id>.md`, `logbooks/<id>.md` dans cette zone |

`adventure.md` contient les consignes communes et entre dans les prompts de tous
les PNJ. Les scénarios réservés au MJ doivent rester dans d'autres dossiers.
La découverte ne charge pas récursivement tous les documents de la campagne.
Chaque PNJ garde son historique, ses notes, ses secrets et son journal propres.
Un identifiant de zone tel que `village/garde` s'écrit entre guillemets dans les
correspondances TOML de voix ou de langue.

Les secrets bloqués ne transmettent que leur `hint:` au modèle. Le MJ autorise
la révélation avec `/yes`, la refuse pour la session avec `/no` ou la reporte
avec `/later`. Le format complet est dans
[src/npc/templates/secrets.md](src/npc/templates/secrets.md).

`import-campaign SOURCE DESTINATION` est un adaptateur spécifique aux anciens
fichiers de CampagneBaston. Il exige une destination neuve ou vide. Une campagne
qui possède déjà `zones/*/characters/*.md` s'ouvre directement.

## Mobile et création de voix

`uv run npc run campaigns/demo --mobile` active le compagnon à
`http://127.0.0.1:8766/` ; son API WebSocket utilise `/ws`.
Le studio enregistre les profils et aperçus vocaux dans `voix/` de la campagne.
Les récits sont déposés dans `histoires/`, avec leur texte Markdown, leur WAV et
leurs métadonnées JSON. Les anciennes bibliothèques `voice_studio/` restent lisibles.
Les WAV déposés directement dans `histoires/` apparaissent aussi dans la bibliothèque,
avec le texte du fichier `.md` de même nom lorsqu'il existe, sans charger de modèle.

Dans l’onglet **Voix**, quatre préréglages proposent des descriptions acoustiques
en anglais, avec une parole en français : **Troll caverneux** (très grave, lent,
guttural et presque monotone), **Elfe** (clair, fluide et posé), **Lutin** (aigu,
vif et espiègle) et **Nain** (grave, rocailleux et ferme).
**Sexe de la voix** permet de choisir **Masculine**, **Féminine** ou de suivre
la description. Ce choix est indépendant du préréglage et reste sélectionné
quand vous changez de préréglage. Il est enregistré avec la voix et repris pour
les récits, où il peut aussi être modifié. Les réglages **Hauteur**, **Musicalité / intonation**,
**Débit de parole**, **Grain** et **Résonance** complètent la description et
priment sur ses propriétés contradictoires. Ils orientent Qwen sans garantir
un timbre précis, notamment pour les créatures fantastiques.
**Préparer la description en anglais** fait traduire vos consignes par le LLM
configuré pour la campagne ; cette option nécessite son serveur, mais ne lui
transmet que la description de voix. Elle reste facultative.

**Transposition** modifie réellement le WAV en demi-tons, sans modifier sa durée
(une valeur négative rend la voix plus grave). **Vitesse audio** modifie la
durée sans modifier la hauteur (`0.9` ralentit, `1.1` accélère). Ces traitements
utilisent les dépendances de l’option `qwen` ; les valeurs extrêmes peuvent
introduire des artefacts. Le préréglage troll démarre à `-3` demi-tons et `0.9×`.
La description originale, la consigne transmise au moteur et les réglages sont
conservés avec le profil. **Ajuster cette voix** permet de repartir d’un aperçu
enregistré ; les récits reprennent ses réglages et permettent de les modifier.
VoiceDesign peut varier d’un passage à l’autre, même avec ces réglages.

Dans **Vos histoires**, toucher **Conserver hors ligne** pendant la connexion au
PC copie le WAV et ses informations sur le téléphone. Attendre la mention
**Disponible hors ligne sur ce téléphone** avant de se déconnecter : l'écoute
utilise alors la copie locale, même si le PC est éteint. **Retirer la copie hors
ligne** libère l'espace du téléphone. Les copies sont séparées par campagne ;
une histoire retirée du PC reste écoutable tant que sa copie locale est conservée.
Le navigateur peut supprimer ces données si son stockage est effacé ou saturé.

Sur l'adresse HTTP du réseau local, garder la page ouverte après le téléchargement.
Pour pouvoir fermer puis rouvrir la page sans réseau, servir le compagnon via
une adresse **HTTPS** de confiance (par exemple avec un proxy local prenant aussi
en charge `/ws`), puis ouvrir cette adresse en ligne une première fois. Le lecteur
prépare alors la réouverture hors ligne et l'indique à l'écran. Le navigateur exige
HTTPS pour cette fonction, sauf sur localhost. Toujours réutiliser la même adresse,
le même port et le même navigateur pour retrouver les copies. Le lien
**Télécharger l'audio** permet aussi de conserver le WAV dans les fichiers du
téléphone pour une écoute dans un lecteur audio, y compris avec un accès HTTP.

Le compagnon est par défaut une télécommande MJ autorisée à approuver les
secrets (`mobile.secret_approval = true`). Utiliser `false` pour les clients
joueurs. L'accès réseau est sans authentification par défaut : réserver l'écoute
LAN à un réseau de confiance, surtout en mode MJ.

Le client Écho peut envoyer `{ "type": "reset_session" }` sur `/ws` depuis
une connexion MJ. Le serveur interrompt la parole et le studio vocal, abandonne
les actions en attente, annule les demandes de divulgation et recrée les états
conversationnels de tous les PNJ. Il vide ensuite les dossiers explicites
`session/` (nom du contrat Écho) et `sessions/` (transcriptions du moteur) de la
campagne active. Aucun chemin fourni par le client n'est utilisé. Les fiches,
le lore, les journaux persistants, les secrets déjà autorisés, les réglages,
les voix et les histoires sont conservés.

La réponse `{ "type": "session_reset" }` est envoyée au demandeur uniquement
après la réussite complète ; Écho peut alors effacer son historique local et se
déconnecter. Une commande refusée ou un échec retourne `error`, sans confirmation
de réussite. Les liens symboliques et jonctions dans les dossiers à vider sont
refusés avant toute suppression. Un appel de modèle déjà bloqué doit revenir
avant la confirmation ; son résultat est abandonné. En cas d'erreur pendant la
suppression, certains fichiers peuvent déjà avoir été retirés ; corriger la
cause puis réessayer la commande.

L’onglet **PNJ · MJ** présente chaque personnage, même inactif : scénario,
attitude, motivation, levier de négociation et nombre de secrets. Une recherche
filtre les noms et scénarios. **Voir les secrets** charge leurs textes pour le
MJ ; cette consultation ne sélectionne pas le PNJ et n’autorise aucune révélation.
Les actions **Présentation**, **Meubler pour attendre** et **Salutation**
sont disponibles sur chaque fiche et sélectionnent le PNJ pour une réplique
fondée sur ses informations. Cette télécommande est réservée aux connexions MJ.
Le serveur accepte `speak_npc` avec `npc` et une `action` facultative ;
`gm_npc_list.speech_actions` fournit les identifiants et libellés disponibles.
Sans action, le PNJ réagit à la situation actuelle. Les anciens champs
`attitude` sont ignorés, y compris ceux envoyés par le client Ionic Écho :
ils n'influencent ni le texte ni la voix.
Les attributs se renseignent directement dans chaque fiche Markdown :

```markdown
- Scénario : Village : la révolte des poules.
- Attitude : Fière, juste.
- Motivation : Obtenir du bon grain et un poulailler réparé.
- Levier de négociation : Une promesse précise devant témoin.
- Introduction : « Bienvenue dans ma boutique de grains. »
- Meuble : « Cette pluie va encore faire gonfler mes sacs. »
- Salutation : « Bonne route, et revenez avant la prochaine récolte. »
```

Les actions mobiles `presentation`, `fill_wait` et `greeting` utilisent respectivement
**Introduction**, **Meuble** et **Salutation** comme référence pour inventer une
nouvelle réplique fidèle à la fiche. Une salutation peut être un au revoir si
la référence le prévoit. Ces trois références peuvent aussi occuper des sections
`## Introduction`, `## Meuble` et `## Salutation`. Seule la référence de l'action
demandée entre dans sa consigne ponctuelle ; elles sont retirées du contexte
permanent et ne deviennent pas des souvenirs. Sans référence, l'action conserve
sa consigne générale. `/reload` prend en compte leurs modifications.

Le nom du fichier définit l'identifiant utilisé par l'application et les voix :
`zones/village/characters/barnabe.md` donne `village/barnabe`, indépendamment d'un
ancien numéro comme `VILLAGE-02` écrit dans la fiche. Après un renommage, adapter
les clés de voix et de langue dans `config.toml`, ainsi que les chemins des
éventuels secrets, journaux et lore propres au PNJ, puis relancer l'application.

Le compteur additionne les confidences de la fiche (`- Cache : ...` ou les
puces sous `## Secrets` / `## Secrets locaux`) et les entrées de son fichier
de secrets soumis au MJ. Garder chaque confidence sur une puce ; les lignes
de suite indentées sont acceptées sous ces sections. Les confidences restent
connues du PNJ selon les conditions de sa fiche ; les secrets bloqués restent
absents des prompts jusqu’à autorisation. Les attributs manquants sont affichés
« Non renseigné ». `/reload` relit les fichiers ; **Actualiser les fiches**
resynchronise l’affichage mobile. Ces informations sont réservées aux connexions
MJ et ne sont pas conservées dans les copies hors ligne.

`--overlay` active l'affichage OBS sur `http://127.0.0.1:8765/`.
L'overlay et le compagnon ont des règles d'exposition distinctes ; voir
[ARCHITECTURE.md](ARCHITECTURE.md).

Pour créer un WAV autonome à partir d'une description de voix :

```sh
uv run npc voice-design /chemin/vers/campagne/voix/conteur.toml
```

Cette commande nécessite l'option `qwen` et crée le WAV à côté du TOML.
`--force` autorise le remplacement d'un WAV existant. Les descriptions `conteur.toml` et `troll.toml` et leurs aperçus sont désormais
dans `/Users/brice/WebstormProjects/CampagneBaston/voix/`. Les créations appartiennent
à la campagne ; le projet moteur ne contient plus de dossier `tools/voice_design/`.

## Développement

```sh
uv run ruff check .
uv run pytest
uv build --no-sources
```

Les tests ordinaires utilisent des doubles et des fichiers temporaires ; aucune
campagne personnelle, aucun microphone ou serveur de modèles n'est nécessaire.
Les tests d'intégration sont séparés : `uv run pytest -m integration`.
Les tests Tk exigent `NPC_TEST_GUI=1` et un affichage de bureau.

Deux scripts manuels restent utiles après modification des prompts :
`uv run python scripts/probe_narration.py [modele]` et
`uv run python scripts/probe_secrets.py [modele] [repetitions]`.
Ils utilisent les modèles réels et les templates intégrés, sans modifier une
campagne personnelle. Leurs résultats nécessitent une lecture humaine.
`uv run python scripts/probe_narration.py [modele] --coherence` utilise des fiches
fictives et examine en français et en anglais qui rend le service et qui paie,
une inversion de rôles à rectifier, un changement explicite de marché et la
distinction entre une offre et un travail déjà accompli. Les attentes sont
affichées pour chaque cas ; cette sonde ne valide pas automatiquement le sens.
Elle couvre aussi l'achat d'une corde apportée par les joueurs, sa vente par le
PNJ et une simple intention de chercher du matériel. Ajouter `--temperature 0.2`
pour comparer ce réglage au comportement du serveur sur les mêmes historiques.

La carte du code est dans [ARCHITECTURE.md](ARCHITECTURE.md) et les conventions
de maintenance dans [AGENTS.md](AGENTS.md). Licence : [AGPL-3.0](LICENSE).

Le compagnon web Écho peut lire le catalogue `studio_list` et télécharger les WAV
avec `GET /studio/audio/<id>.wav` pour une écoute hors connexion. Ces réponses
audio autorisent les lectures HTTP entre origines (CORS, sans cookies) ; le
contrôle d’accès optionnel du serveur reste appliqué avant tout accès au fichier.
