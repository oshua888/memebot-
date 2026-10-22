# Tournée Générale

Appli web de soirée (installable sur téléphone) : jeux à boire et suivi d'alcoolémie en direct entre amis.
Aucun compte, aucune dépendance externe obligatoire : tout est dans ce dossier.

## Contenu
- **Jeux** : Action ou vérité, Je n'ai jamais, Qui pourrait… (avec roue), Le jeu du Roi, Le jeu des deux dés.
- **Alcoolémie** : estimation Widmark minute par minute, courbe, pic, heure de passage sous la limite (0,5 g/L, ou 0,2 g/L en jeune permis) et retour à 0.
- **Amis** : un code de groupe ; chaque téléphone partage son taux en direct via un broker MQTT public (`broker.emqx.io`).

## Mettre en ligne
Il suffit d'héberger ce dossier en HTTPS (GitHub Pages, Netlify, Vercel…).
Avec GitHub Pages : *Settings → Pages → Deploy from a branch*, choisir la branche et le dossier.
Ensuite, sur le téléphone : ouvrir le lien puis « Ajouter à l'écran d'accueil ».

Pour tester en local : `python3 -m http.server` dans ce dossier, puis http://localhost:8000.

## Fichiers
- `index.html` : l'appli
- `mqtt.min.js` : MQTT.js 5.10.1 (licence MIT), pour la synchro de groupe
- `sw.js`, `manifest.webmanifest`, `icon-*.png` : installation et fonctionnement hors ligne

L'estimation d'alcoolémie ne remplace pas un éthylotest.
