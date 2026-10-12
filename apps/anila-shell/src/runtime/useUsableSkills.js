// Skills the signed-in user can apply (`GET /api/skills?view=usable`).
// The composer's "/" picker and skill chips read this list; reloadUsableSkills()
// is called after the skill manager saves. The response may also carry the
// governance stacking limit (`max_stack`); without it the default applies.

import { useCallback, useEffect, useRef, useState } from "react";
import { DEFAULT_MAX_SKILL_STACK, listUsableSkillsWithLimits, setSkillFavorite } from "./skills.js";

export function useUsableSkills(authRequest, isAuthenticated) {
  const [usableSkills, setUsableSkills] = useState([]);
  const [skillStackLimit, setSkillStackLimit] = useState(DEFAULT_MAX_SKILL_STACK);
  const seq = useRef(0);
  const reloadUsableSkills = useCallback(() => {
    const mine = ++seq.current;
    if (!isAuthenticated) {
      setUsableSkills([]);
      setSkillStackLimit(DEFAULT_MAX_SKILL_STACK);
      return Promise.resolve();
    }
    return listUsableSkillsWithLimits(authRequest)
      .then(({ skills, maxStack }) => {
        if (mine !== seq.current) return;
        setUsableSkills(skills);
        setSkillStackLimit(maxStack);
      })
      .catch(() => {
        if (mine === seq.current) setUsableSkills([]);
      });
  }, [isAuthenticated, authRequest]);
  useEffect(() => {
    reloadUsableSkills();
  }, [reloadUsableSkills]);

  // 釘選：先改畫面，失敗再改回來；成功後重抓一次讓排序跟後端一致。
  const toggleSkillFavorite = useCallback(async (skill) => {
    if (!skill || !Number.isInteger(skill.id)) return false;
    const next = !skill.favorite;
    setUsableSkills((rows) => rows.map((row) => (row.id === skill.id ? { ...row, favorite: next } : row)));
    try {
      await setSkillFavorite(authRequest, skill.id, next);
      void reloadUsableSkills();
      return true;
    } catch {
      setUsableSkills((rows) => rows.map((row) => (row.id === skill.id ? { ...row, favorite: !next } : row)));
      return false;
    }
  }, [authRequest, reloadUsableSkills]);

  return { usableSkills, reloadUsableSkills, skillStackLimit, toggleSkillFavorite };
}
