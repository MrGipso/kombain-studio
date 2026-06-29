import React from 'react';
import {interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps} from '../types';
import {MapBackdrop} from './primitives';

export const MapSplit: React.FC<MotionGraphicProps> = ({
  title = 'Не космос, а океан',
  subtitle = 'Видимая технология скрывает физическую инфраструктуру',
  leftLabel = 'Миф: спутники',
  rightLabel = 'Реальность: кабели',
  accent = palette.accent,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const split = interpolate(frame, [fps * 0.7, fps * 3.2], [0, 960], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  return (
    <div style={baseScene}>
      <div style={{position: 'absolute', inset: 0, filter: 'hue-rotate(28deg) saturate(0.7)', opacity: 0.8}}>
        <MapBackdrop />
        <div style={{position: 'absolute', inset: 0, background: 'radial-gradient(circle at 52% 45%, #f9731633, transparent 34%)'}} />
      </div>
      <div style={{position: 'absolute', inset: 0, width: split, overflow: 'hidden'}}>
        <div style={{position: 'absolute', left: 0, top: 0, width: 1920, height: 1080, filter: 'saturate(1.25)'}}>
          <MapBackdrop />
          <div style={{position: 'absolute', inset: 0, background: 'radial-gradient(circle at 45% 48%, #38bdf844, transparent 36%)'}} />
        </div>
      </div>
      <div
        style={{
          position: 'absolute',
          left: split - 3,
          top: 190,
          width: 6,
          height: 720,
          background: accent,
          borderRadius: 999,
          boxShadow: `0 0 28px ${accent}`,
        }}
      />
      <div style={{position: 'absolute', left: 0, right: 0, top: 76, textAlign: 'center'}}>
        <div style={{fontSize: 56, lineHeight: 1.05, fontWeight: 820}}>{title}</div>
        <div style={{fontSize: 28, color: palette.muted, marginTop: 18}}>{subtitle}</div>
      </div>
      <div style={{position: 'absolute', left: 170, bottom: 120, fontSize: 42, fontWeight: 820, color: accent}}>{rightLabel}</div>
      <div style={{position: 'absolute', right: 170, bottom: 120, fontSize: 42, fontWeight: 820, color: palette.warning}}>{leftLabel}</div>
    </div>
  );
};
