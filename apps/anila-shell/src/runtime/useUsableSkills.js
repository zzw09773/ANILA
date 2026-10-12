// Skills the signed-in user can apply (`GET skills?scope=usable`).
// The composer's skill chips and the upcoming "/" skills picker read this list;
// reloadUsableSkills() is called after the skill manager saves.

import { useCallback, useEffect, useState } from "react";
import { listSkills } from "./skills.js";

export function useUsableSkills(authRequest, isAuthenticated) {
  const [usableSkills, setUsableSkills] = useState([]);
  const reloadUsableSkills = useCallback(() => {
    if (!isAuthenticated) {
      setUsableSkills([]);
      return;
    }
    listSkills(authRequest, "usable")
      .then((rows) => setUsableSkills(rows))
      .catch(() => setUsableSkills([]));
  }, [isAuthenticated, authRequest]);
  useEffect(() => {
    reloadUsableSkills();
  }, [reloadUsableSkills]);
  return { usableSkills, reloadUsableSkills };
}
