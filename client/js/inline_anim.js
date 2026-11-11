// Reply-carried inline custom animations (docs/SELF_ANIMATION.md).
//
// A hub preview action arrives as {type:'gesture', custom:true, name,
// preview:true, animation:{...validated doc...}} in the SAME 'say' event as
// the reply — before the desktop's async animations_changed refresh can land
// (the live curtsey_twirl bug: director.say() dropped the name with
// "unknown action — ignored"). The page registers the inline copy into
// customAnims + director.library BEFORE calling director.say(), so the name
// validates and the pose plays the same turn.
//
// Pure module: the page injects its customAnims Map, its library() seam and
// keyframes.js validateAnimation, so this runs under `node --test client/js/`
// with no DOM and no three.js.
//
// Contract: inline copies are PREVIEW material (drafts included — a draft is
// perfectly playable as a preview; only KEPT animations get offered for
// later [act:] tags, and that gate lives on the hub). An inline copy never
// overwrites a real list entry loaded from /custom-animations.json.

/** Actions of this reply that carry a usable inline animation. */
export function collectInlineAnimations(actions) {
  const out = [];
  for (const a of Array.isArray(actions) ? actions : []) {
    if (!a || a.custom !== true || typeof a.name !== 'string' || !a.name) continue;
    const anim = a.animation;
    if (!anim || typeof anim !== 'object' || Array.isArray(anim)) continue;
    out.push({ name: a.name, animation: anim });
  }
  return out;
}

/**
 * Register every inline animation this reply carries.
 * `customAnims` is the page's Map(name -> animation doc); `library(extra)` is
 * the director.library seam ({name: {loop, duration}}); `validateAnimation`
 * is keyframes.js's validator (returns the list of problems).
 * Returns the names newly registered. Junk never raises: an animation that
 * fails validation is left to the /custom-animations.json path.
 */
export function registerInlineAnimations(actions, { customAnims, library, validateAnimation }) {
  const registered = [];
  for (const { name, animation } of collectInlineAnimations(actions)) {
    if (customAnims.has(name)) continue;   // the desktop's own list owns the name
    // trust the action's name (it is what director.say() validates); keep a
    // mismatched doc consistent rather than dropping it
    const anim = animation.name === name ? animation : { ...animation, name };
    if (!Number.isFinite(Number(anim.duration_s)) || Number(anim.duration_s) <= 0) continue;
    if (validateAnimation(anim).length !== 0) continue;
    customAnims.set(name, anim);
    library({ [name]: { loop: !!anim.loop, duration: Number(anim.duration_s) } });
    registered.push(name);
  }
  return registered;
}
