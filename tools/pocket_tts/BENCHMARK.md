# Pocket TTS — premières mesures locales, 16 octobre 2026

Windows 11, CPU du PC, Pocket TTS 3.3.0, PyTorch CPU 2.6.0, modèle français
rapide à 6 couches, voix Estelle. Ces résultats portent sur deux essais dans
le même processus et ne constituent pas une moyenne générale.

Texte : « Bonjour, voyageurs. Asseyez-vous près du feu, je vais vous raconter une histoire. »

| Mesure | Première synthèse | À chaud |
| --- | ---: | ---: |
| Premier bloc PCM disponible | 0,314 s | 0,110 s |
| Premier bloc envoyé à PortAudio | 0,538 s | 0,333 s |
| Génération complète | 4,083 s | 1,221 s |
| Durée audio générée | 4,40 s | 4,40 s |
| Temps de génération / durée audio | 0,93 | 0,28 |
| Sous-alimentations signalées par PortAudio | 0 | 0 |

Délais hors chargement et préparation vocale. Premier téléchargement + chargement :
53,72 s ; première préparation Estelle : 3,48 s. Les essais suivants réutilisent
le modèle et l'état vocal. La génération audio est native et progressive, sans
découpage manuel préalable en phrases. À chaud, les blocs observés durent 80 ms.

La sortie détectée est `Speakers (Realtek(R) Audio)`, sans erreur de lecture.
L'envoi à PortAudio ne mesure pas physiquement le premier son dans les écouteurs.
Whisper reconnaît la réplique française, avec une variation singulier/pluriel
sur « voyageur(s) ». La qualité et les accents restent à apprécier à l'écoute.

Une seconde voix prédéfinie, Marius, a aussi généré correctement une autre réplique
française : premier bloc 0,146 s, 2,16 s d'audio générées en 1,71 s à sa première
synthèse dans ce processus. Le modèle 24 couches n'a pas été téléchargé ni testé.

## Clonage personnel

L'essai a chargé les poids **sans clonage**, accessibles sans compte. Les voix
prédéfinies fonctionnent. Un extrait local personnalisé a été essayé : la bibliothèque
refuse cette fonction et indique qu'il faut accepter les conditions des poids de
clonage sur Hugging Face puis connecter son compte. Aucune condition n'a été
acceptée et aucun compte n'a été configuré par la démo.

## Vérifications

- Génération et lecture progressives réelles, voix Estelle et Marius.
- Cinq tests sans modèles ni matériel : transmission immédiate des blocs, annulation,
  gestion d'erreur, cache des états vocaux et message explicite pour le clonage indisponible.
- Ruff : aucune erreur ; fenêtre Tk et disposition des commandes vérifiées.
- Lanceur verrouillé vérifié, avec mode UTF-8 Windows activé.

WAV et mesures détaillées en JSON : `outputs/` (ignoré par Git).
