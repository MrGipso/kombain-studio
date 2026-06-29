import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {MotionGraphic} from './MotionGraphic';
import type {MotionGraphicProps} from './types';

const defaultProps: MotionGraphicProps = {
  template: 'ChapterTitle',
  durationSeconds: 8,
  fps: 25,
  chapter: 'Пример',
  title: 'Pipeline Studio',
  subtitle: 'Universal content production pipeline',
};

const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="MotionGraphic"
      component={MotionGraphic}
      width={1920}
      height={1080}
      fps={25}
      durationInFrames={200}
      defaultProps={defaultProps}
      calculateMetadata={({props}) => {
        const fps = props.fps ?? 25;
        const durationSeconds = props.durationSeconds ?? 8;
        return {
          fps,
          durationInFrames: Math.max(1, Math.round(durationSeconds * fps)),
        };
      }}
    />
  );
};

registerRoot(RemotionRoot);
