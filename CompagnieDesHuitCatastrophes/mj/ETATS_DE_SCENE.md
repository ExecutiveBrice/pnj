# États à transmettre aux PNJ — aide du MJ

Ces blocs sont à adapter et saisir comme notes hors personnage dans l'outil,
après avoir sélectionné le bon PNJ. Ils ne sont pas automatiquement lus.
L'application garde une mémoire indépendante par personnage : reporter les
mêmes faits aux interlocuteurs concernés, sans transmettre leurs secrets.

## Début d'une scène

```text
[MJ — ÉTAT DE SCÈNE]
Zone et PNJ : VILLAGE / VILLAGE-01 Gertrude.
Moment : arrivée à la grange, avant accord.
Ce que tu as personnellement appris : les visiteurs cherchent un éclat.
Fragment : dans ton nid, pas encore retiré.
Accord : aucun.
Effets actifs : tu peux encore parler.
Document cartographique attribué : aucun pour le moment.
```

## Accord accepté, dernière réplique avant retrait

```text
[MJ — MISE À JOUR]
Le maire a livré le vrai grain et signé la réparation devant toi.
Tu acceptes l'accord et autorises la prise du fragment.
Le fragment n'est pas encore retiré : tu peux prononcer ta dernière réplique.
```

Après le retrait, basculer immédiatement sur un autre PNJ : les poules, les
créatures dont la parole dépend de l'éclat et les épouvantails cessent de
parler ou d'être animés selon la scène. Ne pas attendre que le modèle gère
seul le silence. Flocon peut dire son dernier « enfin » juste avant le retrait.
Les êtres parlants autonomes et le grelot traducteur de Biquette restent
soumis aux faits du livret et aux annonces du MJ.

## Remise du prochain support

```text
[MJ — MISE À JOUR]
La situation locale est résolue. Tu détiens maintenant le support 2 :
un calque à quatre cercles, avec les codes UDW, VHO, FRT et IHU.
Tu le remets gratuitement aux héros. Explique uniquement l'alignement
sur les marques de la carte. Tu ignores la clé, l'énigme et la destination.
```

Le numéro 2 est un exemple : choisir 1, 2 ou 3 selon la progression réelle.
Après la remise, préciser que l'objet est désormais dans les mains des héros.
Pour le support 3, fournir seulement la notice à remettre d'INDICES.md ; ne
pas copier son tableau de solutions dans le contexte d'un PNJ.

## Entrée dans le laboratoire

Sélectionner Archibald, puis transmettre :

```text
[MJ — MISE À JOUR]
Les héros ont trouvé l'accès par leur enquête et sont au pupitre du vestibule.
Tu es derrière une barrière, retenu loin du pupitre. Les commandes CONFIRMER
et ANNULER sont lisibles. Tu ne peux empêcher physiquement les manipulations.
Une plume dépasse de ton chapeau. La preuve de ton erreur est visible.
```

Autoriser le secret `vrai-souhait` via la demande de révélation, puis `/yes`
en console (ou le contrôle d'approbation du MJ). Ne pas saisir `/yes` avant
qu'une demande soit en attente. La révélation est conservée dans secrets.md ;
pour rejouer avec un autre groupe, dupliquer une campagne vierge ou enlever
sa ligne `revealed:` et repartir sans les journaux de l'ancien groupe.
Annoncer ensuite les manipulations réussies et l'état réel du mécanisme.
