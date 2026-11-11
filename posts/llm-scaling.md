---
title: scaling engineering workflows with LLMs
date: 11/10/2026
tags: engineering
---

something that ive been thinking about recently is designing scalable development workflows to have a stable codebase in the realm of parallel coding agents.

only a couple months ago, LLMs required constant supervision. every single major feature had to be reviewed, QAd locally and on dev servers, and each agent interaction had to be evaled and iterated on.

nowadays, every PR goes through multiple agentic review loops. changes are evaluated autonomously in VMs and sandboxes, evals are run and approved by agents themselves. nothing requires a human in the loop.

everything has become autonomous. agentic engineering has pushed software to a point where the process of development is self-healing.

however, the biggest issue that ive faced working at a company where all software engineers revel in agentic workflows is context (and CI).

## tests are going to be the end of me
merge conflicts are the biggest pain the ass. everything is constantly pushing code, so the first PR to merge causes the rest in the queue to conflict.

you then have to rebase onto main, rerun CI, and go through another agentic review round.

CI finally passes? well too bad, an agentic reviewer found a `P1` in the code that you wrote, and your agent is already halfway through working on a new commit.

i hate CI. i also hate how much agents love writing tests. why is that the initial direction for all PRs is writing "fail first tests." by default, a 2k line diff is filled with 800 lines of tests. 

the thing is that agents are so good at managing merge conflicts, fixing tests, just the entire process of development, so engineers dont really pay attention to the details anymore. 2 years ago, an annoying test that was holding up CI would have been refactored/removed by the first engineer that was annoyed at it.

now, everyone just tells their agents to babysit their PRs.

## parallel development
in relation, since LLMs are so incredibly good at development, no one "owns" specific parts of a project naymore.

an issue found in project A can (and will be) fixed by the "lead" of project B. because things are so much easier to fix, everyone does it. the main problem with this that ive discovered is context. a simple `@Devin RCA` can derail someones internal roadmap, and ultimately introduces more issues.

the great thing about companies that focus on agentic UX (like cognition with Devin) is the ease of use. 

the bad thing about companies that focus on agentic UX is the ease of use.

agents can now interact with you across multiple surfaces. start one chat on your laptop, continue on your way home from work. theres an issue that someone posted about in slack? well, lets fix it using our grokbot.

you can spin up an agent to do anything you can possibly dream of, as long as you have internet access.

im not saying this is a bad thing. at work, im notoriously known to just enjoy working on several different projects at once. im saying its a major oversight in the organization of agentic engineering.

everyone thinks slightly differently about the severity of problems.

an issue that was reported in slack could have been the one in a million case that the engineer didnt decide to build support for, since it would have added another layer of complexity.

however, issues can now be fixed with agents, *easily*. so a pr is opened, and its approved, and it makes its way over to main. 

the main problem with this is agents are primarily outcome focused. an UI issue is fixed with a couple lines of CSS and 10 more lines of tests, but a page that the agent missed is affected, and no one catches it until a customer goes on that page.

an intercom message comes in, everyones scrambling, pointing fingers, even though everyone is susceptible to shipping slop and it just happened to be you last night.
