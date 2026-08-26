# Moods-Happy-and-Sad-Clusters
Differentiating random Spotify's Happy and Sad playlists with Valence, Energy and Thayer's 2D emotion model

**Note (2026):** the original notebook's Spotify calls (`sp.user_playlists`, `sp.audio_features`)
no longer work — Spotify deprecated `audio_features`/`audio-analysis` for apps without
pre-existing extended-quota access (Nov 2024), and removed access to other users' playlists
entirely in its Feb 2026 API changes. `Moods - Happy and Sad Clusters (v2 - Essentia+iTunes prototype).ipynb`
is a working replacement: iTunes Search API for audio previews (public, no key) + Essentia's
pretrained DEAM model for valence/arousal, instead of the Spotify Web API.
