import React, {useMemo} from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {svg, type Scene} from '../../../linkwork/src/index.ts';

// A Linkwork scene (jointed figures and mechanisms, ~/linkwork): every frame is drawn from the scene data.
export const LinkworkScene: React.FC<{scene: Scene; transparent?: boolean}> = ({scene, transparent}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const markup = useMemo(() => svg(scene, frame / fps, transparent ? {background: false} : {}), [scene, frame, fps, transparent]);
  return <AbsoluteFill dangerouslySetInnerHTML={{__html: markup}}/>;
};
