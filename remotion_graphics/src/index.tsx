import React from 'react';
import {Composition, registerRoot, CalculateMetadataFunction} from 'remotion';
import {MotionGraphic} from './MotionGraphic';
import {VideoComposer, VideoComposerProps} from './Composer';
import type {MotionGraphicProps} from './types';

const defaultProps: MotionGraphicProps = {
  template: 'ChapterTitle',
  durationSeconds: 8,
  fps: 25,
  chapter: 'Пример',
  title: 'Pipeline Studio',
  subtitle: 'Universal content production pipeline',
};

const calculateComposerMetadata: CalculateMetadataFunction<VideoComposerProps> = async ({
  props,
}) => {
  const cuts = props.cuts ?? [];
  if (cuts.length === 0) {
    return {durationInFrames: 30};
  }
  const lastEnd = Math.max(...cuts.map((c) => c.out_seconds ?? 0));
  return {durationInFrames: Math.max(30, Math.ceil((lastEnd + 1) * (props.fps ?? 25)))};
};

const RemotionRoot: React.FC = () => {
  return (
    <>
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
      <Composition
        id="VideoComposer"
        component={VideoComposer}
        width={1920}
        height={1080}
        fps={25}
        durationInFrames={30 * 60}
        defaultProps={{cuts: []} as VideoComposerProps}
        calculateMetadata={calculateComposerMetadata}
      />
    </>
  );
};

registerRoot(RemotionRoot);
