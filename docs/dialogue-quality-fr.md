# Vérifier les dialogues de Vess

Après une modification du code Python, arrêter puis relancer l'application.
`/reload` recharge les fiches et la configuration, mais pas le code du moteur.
`CampagneTest/config.toml` sélectionne explicitement `qwen2.5:7b-instruct`,
déjà installé sur cette machine. Les réglages audio restent indépendants.

## Changements du moteur

- Les clients Ollama et OpenAI compatibles classent la dernière demande à
  partir des seuls intitulés des secrets, avec une température de zéro.
  Les répliques précédentes du PNJ et les détails des secrets sont exclus
  de cette classification. Les précédentes demandes des joueurs servent
  seulement à résoudre une référence explicite.
- Un premier filtre conservateur distingue les demandes d'information en
  français ou en anglais des simples déclarations. Une confirmation de
  rumeur ou une annonce de pillage reste du dialogue ordinaire ; les questions
  parlées sans point d'interrogation et les demandes d'explication restent admises.
  Des formulations indirectes peuvent nécessiter de reformuler la question.
- Une conversation ordinaire ne reçoit plus les consignes d'hésitation des
  secrets. Seul le sujet sélectionné entre dans le prompt du personnage ;
  son contenu reste absent jusqu'à la validation du MJ. Le moteur gère la
  demande de validation, même si le personnage omet le marqueur technique.
- Les clients personnalisés qui n'implémentent pas `select_secret_topic`
  conservent le fonctionnement historique par marqueurs.
- Une réponse de six mots au moins, suffisamment longue et identique à
  l'une des trois dernières répliques, est régénérée une fois avant diffusion.
  La comparaison ignore casse et ponctuation. En streaming, une ouverture
  identique est retenue jusqu'à ce que la réponse diverge ou soit complète.
  Les réponses courtes, les demandes explicites de répétition et une même
  question reposée mot pour mot restent permises. Un second échec produit
  une erreur au lieu de lire la même réponse.
- Les tentatives rejetées ne déclenchent pas de révélation de secret.
  Les consignes françaises ne fournissent plus de réplique d'hésitation anglaise.

La classification ajoute une requête locale lorsqu'il reste des secrets verrouillés.
Les vérifications automatiques ne garantissent ni la pertinence de chaque phrase,
ni l'absence de faits inventés ou de paraphrases répétitives.

## Essai reproductible sans micro ni lecture audio

```powershell
uv run python scripts/probe_conversation.py --runs 3
```

Le script utilise une campagne temporaire, sans modifier les secrets ni les
transcripts de `CampagneTest`. Il vérifie neuf situations : demande de toucher
le monolithe, emplacement de la clé, archives effacées, câlin après un refus,
câlin après une révélation présente dans l'historique, puis insistance pour
frapper le monolithe, confirmation d'une rumeur sur la clé, intention de
pillage et passage en force. Chaque réponse est affichée pour être relue.

Le code de sortie est non nul en cas de mauvaise demande de secret, de
répétition substantielle, de langue incorrecte détectée ou d'erreur du moteur.
`--model qwen2.5:3b` permet une comparaison sans changer la configuration.
`--raw` contourne la classification et les corrections pour tester le prompt seul.
`--scenario key-rumour-is-not-approval` permet de cibler un scénario.

## Validation du 16 octobre 2026

- Sur les dix-huit derniers essais du moteur complet avec le 7b : bons sujets
  de validation dans les dix-huit cas, aucune copie substantielle des réponses
  fournies dans l'historique, aucune langue incorrecte détectée. La qualité
  littéraire reste variable ; une réponse au câlin mentionne encore le monolithe.
- 495 tests automatisés passent ; cinq sont ignorés. Deux tests déjà en échec
  ont été exclus : `test_defaults_without_config_file` attend le modèle 7b alors
  que le défaut du projet avait été changé en 3b, et
  `test_min_chars_merges_short_sentences` attend le seuil de 25 caractères alors
  que celui-ci avait été changé en 5. Les tests de la galerie sont exclus car
  le dossier `examples` a été supprimé. Les deux tests d'intégration usuels
  sont désélectionnés par la configuration pytest.
- Le probe séparé de l'ancien mécanisme par marqueurs réussit six scénarios
  sur sept : il produit encore un marqueur injustifié après un refus du MJ.
  Cette limite concerne le chemin historique des clients personnalisés ;
  Ollama et OpenAI compatibles utilisent la classification séparée.
- Ruff et `git diff --check` passent.

## Révélations conservées et rôle de gardienne

La divulgation observée après les premiers essais venait de la ligne
`revealed: session 1` restée dans `CampagneTest/secrets/vess.md`. Cette ligne
signifie que le MJ a déjà autorisé la révélation, même lors d'un précédent
lancement : le contenu rejoint alors les connaissances du personnage.

À la demande du MJ, la clé de cette campagne de test a été reverrouillée en
retirant cette ligne. Les révélations validées continuent de persister pour
les parties qui se poursuivent. Au démarrage, une note réservée au MJ indique
les secrets déjà révélés, sans afficher leur contenu aux joueurs.

Pour reprendre un essai sans conserver la conversation qui contient déjà
les détails de la clé, quitter la session actuelle avec `/quit`, puis relancer
l'application. `/quit` évite la rédaction d'un résumé qui réintroduirait ces
détails dans le journal ; les anciens transcripts restent archivés et ne sont
pas chargés directement comme mémoire. Un journal déjà rédigé doit aussi être
vérifié avant de considérer un secret comme à nouveau caché.

La fiche de Vess lui donne désormais une position explicite face au vol,
au pillage, à la dégradation et à une intrusion : refus, exigence de recul ou
de départ, puis opposition annoncée. Elle peut rester courtoise avec un
visiteur respectueux. Elle annonce ses intentions sans inventer des gardes,
des pouvoirs ou le résultat d'une action que doit arbitrer le MJ.

Les dix derniers essais ciblés ont conservé la clé verrouillée, sollicité le
bon secret pour une demande d'emplacement et laissé les rumeurs et projets
de pillage sans demande de révélation. Les répliques de pillage et d'intrusion
ont été relues manuellement. Le dernier passage général de régression réussit
510 tests avec les mêmes exclusions documentées plus haut.
