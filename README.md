# MemeBot — simulation

Scanner de meme coins + bot d'achat/vente **en argent fictif**, 4 stratégies comparées
(dont une qui achète **au hasard**, pour savoir si le score sert à quelque chose).

- Tourne sur GitHub Actions : chaque lancement dure ~5h30, un cycle par minute.
- Tableau de bord publié sur GitHub Pages, données lues dans `state.json`.
- `trades.csv` : chaque trade fermé avec les mesures du token à l'achat.
- `python memescanner.py analyse` : quelles mesures annonçaient les gains.

Coûts simulés : 1 % de frais + impact sur le prix selon la liquidité du token.
Aucun achat réel, aucune clé de portefeuille. Les résultats passés ne prédisent rien.
