#!/usr/bin/env bash
# Writes data/contributions.json and data/repos.json from the GitHub GraphQL API.
# Used by the daily workflow; also works locally if `gh auth login` is done.
#   PROFILE_LOGIN=yamenkabalan bash scripts/fetch-data.sh
set -euo pipefail
cd "$(dirname "$0")/.."
LOGIN="${PROFILE_LOGIN:-yamenkabalan}"
mkdir -p data

CAL_QUERY='query($login:String!){
  user(login:$login){ contributionsCollection{ contributionCalendar{
    totalContributions
    weeks{ contributionDays{ date contributionCount weekday } }
  } } }
}'

gh api graphql -f query="$CAL_QUERY" -f login="$LOGIN" | jq --arg login "$LOGIN" '
  .data.user.contributionsCollection.contributionCalendar as $c
  | { login: $login,
      generated: (now | todate),
      total: $c.totalContributions,
      days: [ $c.weeks[].contributionDays[]
              | { date, count: .contributionCount, weekday } ] }
' > data/contributions.json

# Featured repos: names come from featured.txt so the cards and the README agree.
FIELDS='name description url stargazerCount forkCount pushedAt primaryLanguage{ name color }'
ALIASES=""; i=0
while IFS= read -r name; do
  [[ -z "${name// }" || "$name" == \#* ]] && continue
  ALIASES+=" r$i: repository(owner:\$login, name:\"$name\"){ $FIELDS }"
  i=$((i + 1))
done < featured.txt

gh api graphql -f query="query(\$login:String!){ $ALIASES }" -f login="$LOGIN" | jq '
  [ .data | to_entries
    | sort_by(.key | ltrimstr("r") | tonumber)
    | .[].value | select(. != null)
    | { name, description, url,
        stars: .stargazerCount, forks: .forkCount, pushedAt,
        language: .primaryLanguage.name, languageColor: .primaryLanguage.color } ]
' > data/repos.json

echo "wrote data/contributions.json ($(jq '.days|length' data/contributions.json) days) and data/repos.json ($(jq 'length' data/repos.json) repos)"
