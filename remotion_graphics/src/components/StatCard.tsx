import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps} from '../types';

export const StatCard: React.FC<MotionGraphicProps> = ({
  title = 'Физический интернет',
  value = '95%+',
  label = 'международного трафика идет по подводным кабелям',
  note = '',
  accent = palette.accent,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame, fps, config: {damping: 18, stiffness: 85}});
  const glow = interpolate(frame, [0, fps * 2], [0.25, 0.55], {extrapolateRight: 'clamp'});

  return (
    <div style={baseScene}>
      <div
        style={{
          position: 'absolute',
          inset: 0,
          background: `radial-gradient(circle at 50% 42%, ${accent}33, transparent 36%)`,
          opacity: glow,
        }}
      />
      <div
        style={{
          position: 'absolute',
          left: 285,
          top: 250,
          width: 1350,
          height: 560,
          borderRadius: 28,
          border: `3px solid ${accent}`,
          background: palette.panel,
          boxShadow: '0 24px 80px rgba(0,0,0,0.32)',
          transform: `scale(${0.92 + enter * 0.08})`,
          opacity: enter,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          padding: 80,
        }}
      >
        <div style={{fontSize: 52, fontWeight: 760, marginBottom: 48}}>{title}</div>
        <div style={{fontSize: 172, lineHeight: 1, fontWeight: 860, color: accent}}>{value}</div>
        <div style={{fontSize: 48, lineHeight: 1.16, fontWeight: 720, marginTop: 46, maxWidth: 1060}}>{label}</div>
        {note ? <div style={{fontSize: 30, lineHeight: 1.24, color: palette.muted, marginTop: 34}}>{note}</div> : null}
      </div>
    </div>
  );
};
