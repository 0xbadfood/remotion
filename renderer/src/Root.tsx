import React from 'react';
import {Composition} from 'remotion';
import {InfoFilm, type FilmData} from './Video';

const placeholder: FilmData = {title:'Informational film', subject:'Subject', width:1920,height:1080,fps:30,durationInFrames:30,scenes:[],captions:[],sources:[]};
export const Root: React.FC = () => <Composition id="InfoFilm" component={InfoFilm}
  width={1920} height={1080} fps={30} durationInFrames={30} defaultProps={{data:placeholder}}
  calculateMetadata={({props})=>({width:props.data.width,height:props.data.height,fps:props.data.fps,durationInFrames:props.data.durationInFrames})}/>;
