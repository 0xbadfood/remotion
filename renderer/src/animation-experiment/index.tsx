import React from 'react';
import {Composition,registerRoot} from 'remotion';
import {AnimationRender,type AnimationRenderProps} from './AnimationExperiment';
import {animationDurationFrames} from './chapters';
const placeholder:AnimationRenderProps={plan:{schema_version:'infofilm.animation-motion.v1',scene_id:'animation-experiment',visual_message:'Declarative scene',fps:30,duration_seconds:1,width:1280,height:720,background:'#071b29',objects:[],captions:[],checkpoints:[]},assets:{}};
const Root:React.FC=()=> <Composition id="AnimationExperiment" component={AnimationRender} defaultProps={placeholder} width={1280} height={720} fps={30} durationInFrames={30}
  calculateMetadata={({props})=>({width:props.plan.width,height:props.plan.height,fps:props.plan.fps,durationInFrames:animationDurationFrames(props.plan)})}/>;
registerRoot(Root);
