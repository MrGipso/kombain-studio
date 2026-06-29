import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps} from '../types';

export const TextOverlay: React.FC<MotionGraphicProps> = ({
  title = 'Текст',
  subtitle = '',
  accent = palette.accent,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 14, stiffness: 60}});
  const fade = interpolate(frame, [0, 15], [0, 1], {extrapolateRight: 'clamp'});

  return (
    <div style={baseScene}>
      <div style={{
        position: 'absolute', inset: 0,
        background: `radial-gradient(circle at 50% 45%, ${accent}22, transparent 60%)`,
        opacity: fade,
      }} />
      <div style={{
        position: 'absolute',
        left: 240, right: 240, top: 320,
        textAlign: 'center',
        transform: `translateY(${(1 - enter) * 40}px)`,
        opacity: enter,
      }}>
        <div style={{
          fontSize: 80,
          lineHeight: 1.12,
          fontWeight: 820,
          color: palette.text,
          marginBottom: 40,
        }}>
          {title}
        </div>
        {subtitle ? (
          <div style={{
            fontSize: 36,
            lineHeight: 1.3,
            color: palette.muted,
            maxWidth: 1200,
            margin: '0 auto',
          }}>
            {subtitle}
          </div>
        ) : null}
      </div>
    </div>
  );
};
