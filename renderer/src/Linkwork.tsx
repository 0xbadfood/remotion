import React, {useMemo} from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {svg, type Scene} from './linkwork/index.ts';

// A Linkwork scene (jointed figures and mechanisms, ~/linkwork): every frame is drawn from the scene data.
// src/linkwork is a link to ~/linkwork/src, so a render snapshot (which copies src) carries the Linkwork it used.
// `speed` above 1 plays a scene a little faster so its motion ends inside its beat (infofilm speed_to_fit).
// `restart`: a replay (infofilm narration_room): from this second of the beat the scene plays again from its start.
export const LinkworkScene: React.FC<{scene: Scene; transparent?: boolean; speed?: number; restart?: number}> = ({scene, transparent, speed = 1, restart}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const seconds = frame / fps;
  const local = restart && seconds >= restart ? seconds - restart : seconds;
  const markup = useMemo(() => svg(scene, local * speed, transparent ? {background: false} : {}), [scene, local, transparent, speed]);
  return <AbsoluteFill dangerouslySetInnerHTML={{__html: markup}}/>;
};
