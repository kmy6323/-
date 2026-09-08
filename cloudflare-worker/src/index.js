const GITHUB_OWNER = "kmy6323";
const GITHUB_REPO = "-";

// event.cron -> 어떤 시장을 스캔할지 매핑
// "0 1 * * 1-5"  : KST 10:00 (한국 개장 1시간 후)
// "30 14 * * 1-5": 미국 동부 서머타임(EDT) 기준 개장 1시간 후
// "30 15 * * 1-5": 미국 동부 표준시(EST) 기준 개장 1시간 후
const MARKET_BY_CRON = {
  "0 1 * * 1-5": "kr",
  "30 14 * * 1-5": "us_agentt",
  "30 15 * * 1-5": "us_agentt",
};

export default {
  async scheduled(event, env, ctx) {
    const market = MARKET_BY_CRON[event.cron];
    if (!market) {
      console.error(`알 수 없는 cron: ${event.cron}`);
      return;
    }

    const res = await fetch(
      `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "scanner-cron-worker",
        },
        body: JSON.stringify({
          event_type: "trigger-scanner",
          client_payload: { market },
        }),
      }
    );

    if (!res.ok) {
      console.error(
        `GitHub repository_dispatch 실패 (market=${market}): ${res.status} ${await res.text()}`
      );
    }
  },
};
