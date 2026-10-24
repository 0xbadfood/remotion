import React from 'react';
import {Composition} from 'remotion';
import {InfoFilm, type FilmData} from './Video';
import {LinkworkScene} from './Linkwork';
import type {Scene} from './linkwork/index.ts';

const placeholder: FilmData = {title:'Informational film', subject:'Subject', width:1920,height:1080,fps:30,durationInFrames:30,scenes:[],captions:[],sources:[]};
const emptyScene: Scene = {linkwork:1,width:1920,height:1080,fps:30,duration:1,props:[],actors:[]};
export const Root: React.FC = () => <><Composition id="InfoFilm" component={InfoFilm}
  width={1920} height={1080} fps={30} durationInFrames={30} defaultProps={{data:placeholder}}
  calculateMetadata={({props})=>({width:props.data.width,height:props.data.height,fps:props.data.fps,durationInFrames:props.data.durationInFrames})}/>
  <Composition id="Linkwork" component={LinkworkScene} width={1920} height={1080} fps={30} durationInFrames={30} defaultProps={{scene:emptyScene}}
  calculateMetadata={({props})=>({width:props.scene.width,height:props.scene.height,fps:props.scene.fps,durationInFrames:Math.ceil(props.scene.duration*props.scene.fps)})}/></>;
