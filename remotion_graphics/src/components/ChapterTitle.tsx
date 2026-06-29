import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps} from '../types';

export const ChapterTitle: React.FC<MotionGraphicProps> = ({
  chapter = 'Глава 1',
  title = 'Интернет появился до интернета',
  subtitle = '',
  accent = palette.accent,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 19, stiffness: 82}});
  const line = interpolate(frame, [12, 52], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});

  return (
    <div style={baseScene}>
      <div
        style={{
          position: 'absolute',
          left: 220,
          top: 276,
          width: 1480,
          transform: `translateY(${(1 - enter) * 34}px)`,
          opacity: enter,
        }}
      >
        <div style={{fontSize: 34, fontWeight: 780, letterSpacing: 0, color: accent, marginBottom: 34}}>{chapter}</div>
        <div style={{fontSize: 86, lineHeight: 1.02, fontWeight: 840, maxWidth: 1280}}>{title}</div>
        {subtitle ? <div style={{fontSize: 32, lineHeight: 1.24, color: palette.muted, marginTop: 36, maxWidth: 980}}>{subtitle}</div> : null}
        <div
          style={{
            width: 760 * line,
            height: 8,
            background: accent,
            borderRadius: 999,
            marginTop: 58,
            boxShadow: `0 0 32px ${accent}88`,
          }}
        />
      </div>
    </div>
  );
};
