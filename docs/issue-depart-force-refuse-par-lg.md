# À régler : départ forcé du lave-linge refusé par LG, sans suite (31/10/2026)

**Version : 0.13.1.** Rien n'est corrigé pour l'instant.

## Ce qui s'est passé

| Heure (locale) | Événement |
|---|---|
| 13:11:29 | Appui sur « Lancer le lave-linge maintenant » (bouton du Routeur solaire) |
| 13:11:33 | La machine est réveillée par l'intégration officielle. Statut `initial`, et `total_time` passe de 86 min à `unknown`. |
| 13:11:34 | SmartThinQ appelle `remote_start`, et LG refuse avec `UseOfficialAPIError: Please consider using the official API` |

Ensuite, plus rien :

- L'exception n'est pas une `HomeAssistantError`, donc ni `SmartThinqStarter.start()` (`smartthinq.py:93`) ni `solar.py:212 _start` ne l'attrapent. Elle finit en « Task exception was never retrieved ».
- Aucune notification n'est envoyée.
- `binary_sensor.lave_linge_smartthinq_connection` reste « connecté ». Le refus n'est pas un échec d'authentification, et `AUTH_FAILURES` ne le reconnaît pas.
- La décision reste bloquée sur « départ forcé » (`_forced`), et le badge « Départ » continue d'attendre.
- La machine ne part pas. Il a fallu la lancer à la main.

## Ce qu'il faut faire

1. **Attraper toute exception** autour de l'appui SmartThinQ, pas seulement `HomeAssistantError`. En cas d'échec :
   - journaliser le refus ;
   - notifier « départ refusé par LG — lancer à la main » ;
   - lever `_forced` pour que l'attente se termine proprement.
2. **Signaler `UseOfficialAPIError`** sur `binary_sensor.lave_linge_smartthinq_connection` : si LG refuse l'API non officielle, plus aucun départ solaire n'est possible. Ajouter le motif à `AUTH_FAILURES`, ou créer un état « refusé » distinct.
3. **À tester, la machine armée et quelqu'un sur place** : un départ par l'intégration officielle (`select` `start`) sur une machine **déjà réveillée**.
   - Le test du 26/10 était parti sur le programme par défaut, mais la machine venait d'être réveillée.
   - Si le programme armé est conservé, l'officielle peut remplacer SmartThinQ.
   - Sinon, le départ solaire dépend du bon vouloir de LG. Le dire dans le README.
4. Couvrir 1 et 2 dans `tests/` avec un `SmartThinqStarter` dont l'appui lève l'exception.
