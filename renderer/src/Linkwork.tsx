import React, {useMemo} from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {svg, type Scene} from './linkwork/index.ts';

// A Linkwork scene (jointed figures and mechanisms, ~/linkwork): every frame is drawn from the scene data.
// src/linkwork is a link to ~/linkwork/src, so a render snapshot (which copies src) carries the Linkwork it used.
export const LinkworkScene: React.FC<{scene: Scene; transparent?: boolean}> = ({scene, transparent}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const markup = useMemo(() => svg(scene, frame / fps, transparent ? {background: false} : {}), [scene, frame, fps, transparent]);
  return <AbsoluteFill dangerouslySetInnerHTML={{__html: markup}}/>;
};
