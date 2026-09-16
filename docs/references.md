# References and design provenance

- TypeSafe launch article: https://typesafe.ai/blog/introducing-system-one-models-and-jev
- Official Doom demo video: https://framerusercontent.com/assets/rlL7ImEbISFoYt3IJEHHfvjpY.mp4
- TypeSafe API: https://docs.typesafe.ai/api
- TypeSafe decision confidence: https://docs.typesafe.ai/confidence
- BottleShip runtime: https://github.com/jenissimo/bottleship
- BottleShip automation harness: https://github.com/jenissimo/bottleship/blob/main/docs/harness.md

The official Doom demonstration receives structured state, not images, and calls
Jev about ten times per second. Its display combines the game with actual action
distributions, confidence, telemetry, and a state-to-command diagram. The launch
article says an in-depth walkthrough is planned; a public Doom harness was not
available when this project was built.

This project independently implements the same broad pattern for StarCraft's
shareware release. The reference video is linked, not redistributed. Probability
bars represent choices among candidate actions; they are not win probabilities.

## Third-party rights

The project code is MIT licensed. BottleShip is Apache-2.0 and includes its own
third-party notices, which remain with its downloaded source. StarCraft and its
shareware game data belong to Blizzard Entertainment and are not covered by this
repository's license. No game executable or game archive is committed here.
