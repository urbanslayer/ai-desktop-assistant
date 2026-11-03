Drop VRMA gesture clips here to replace the built-in gestures.

A file named `wave.vrma` (or `wave.vrma.glb`) takes over the built-in wave;
same for `think`, `listen`, `talk`, `stretch`, `look_around`, `fidget`,
`nod`, `shake`, `laugh`, `pout`, `point`, `shrug`. Anything else in this
folder is listed by /animations.json but not used automatically.

Free VRMA sources (check each pack's licence before shipping it):
- pixiv's VRM Animation Sample: https://booth.pm/ja/items/5512385 (free, but
  the download asks for a BOOTH login, so it can't be bundled here silently)
- VRoid Hub motion tags: https://hub.vroid.com/en/tags/4530169210384817001

The clips are loaded with @pixiv/three-vrm-animation and re-targeted to the
active persona on every swap, exactly like the built-in pose gestures.
