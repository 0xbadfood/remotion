import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {GravitySeed, type GravityProps} from './GravitySeed';
const placeholder:GravityProps={plan:{id:'gravity-seed',title:'Gravity',visual_message:'Gravity',width:1280,height:720,fps:30,duration_seconds:21,physics:{gravity_m_s2:9.81,release_height_m:8,launch_speed_m_s:12,drop_x_m:.5},objects:[],animation:{first_launch:{start_seconds:3,end_seconds:8},comparison:{start_seconds:12,end_seconds:17},trajectory_intervals:6,vertical_acceleration_arrow:true,replay_fade:{start_seconds:10.7,end_seconds:11.5}},cues:[]},assets:{}};
const Root:React.FC=()=> <Composition id="GravitySeed" component={GravitySeed} width={1280} height={720} fps={30} durationInFrames={630} defaultProps={placeholder}
  calculateMetadata={({props})=>({width:props.plan.width,height:props.plan.height,fps:props.plan.fps,durationInFrames:Math.round(props.plan.duration_seconds*props.plan.fps)})}/>;
registerRoot(Root);
