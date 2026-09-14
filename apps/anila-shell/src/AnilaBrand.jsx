// ANILA 品牌資產（public/brand）。路徑一律吃 Vite BASE_URL，
// 正式部署 base 是 `/anila/`，本機 dev 是 `/`。
import React from "react";

function brandAsset(filename) {
  const base = import.meta.env.BASE_URL || "/";
  const prefix = base.endsWith("/") ? base : `${base}/`;
  return `${prefix}brand/${filename}`;
}

export const ANILA_LOGO_PNG = brandAsset("anila-logo.png");
export const ANILA_MARK_PNG = brandAsset("anila-mark.png");
export const ANILA_LOGO_MP4 = brandAsset("anila-logo.mp4");

export function AnilaLogoImg({
  variant = "logo",
  height,
  width,
  alt = "ANILA",
  className = "",
  style,
  ...rest
}) {
  const src = variant === "mark" ? ANILA_MARK_PNG : ANILA_LOGO_PNG;
  const resolvedHeight = height ?? (variant === "mark" ? 20 : 32);
  return (
    <img
      src={src}
      alt={alt}
      draggable={false}
      className={["anila-brand-img", className].filter(Boolean).join(" ")}
      style={{
        display: "block",
        height: resolvedHeight,
        width: width ?? "auto",
        objectFit: "contain",
        ...style,
      }}
      {...rest}
    />
  );
}

export function AnilaLogoVideo({
  width = 180,
  poster = ANILA_LOGO_PNG,
  className = "",
  style,
  ...rest
}) {
  const silence = (el) => {
    if (!el) return;
    el.muted = true;
    el.defaultMuted = true;
    el.volume = 0;
  };
  return (
    <div
      className="anila-brand-video-wrap"
      style={{ width, height: width, margin: "0 auto" }}
    >
      <video
        src={ANILA_LOGO_MP4}
        poster={poster}
        autoPlay
        muted
        playsInline
        loop
        preload="auto"
        controls={false}
        disablePictureInPicture
        aria-label="ANILA"
        className={["anila-brand-video", className].filter(Boolean).join(" ")}
        ref={silence}
        onLoadedMetadata={(e) => silence(e.currentTarget)}
        style={{
          display: "block",
          width: "100%",
          height: "auto",
          background: "transparent",
          ...style,
        }}
        {...rest}
      />
    </div>
  );
}
