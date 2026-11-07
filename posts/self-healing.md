---
title: thoughts on a self-healing harness 
date: 11/06/2026
tags: engineering
---

> thoughts on productionizing my agent harness

i don't think anyone will ever use my agent harness. distribution was, and never will be, the point of making it.

however, ive been thinking about productionizing it. not the general public, but rather, hosted locally on a home server.

i have a rough vision of the future of agent automation.

## the dream
the coolest agentic products out there right now are just really good agent harnesses. theres a lot of nuance with agent harnesses: logging, traces, evals. the dream is to be able to replicate any agentic product, and deploy it right away to your services, in one prompt:

> "build braintrust locally. ingest all of my agent traces from X, build analytics and a judge pipeline, surface all urgent results through my messaging agent. add a dashboard for judge verdicts"

> "build an IDE in which an agent guides me through leetcode problems until i master graph theory."

this would be the coolest thing ever.

a surface for flexible agent harnesses.

im surprised this isnt something abundantly popular nowadays: self-hosted agentic pipelines.

i feel like im only now scratching the surface of agentic engineering. 

## a rough mental model
my [harness](/posts/harness) has been my daily driver for months. i control everything going into and coming out of said harness, and i feel like just that alone has allowed me to do so much regarding homebrewed agent harness for different cases (autonomous judging + evals, a messaging agent, etc).

however, everytime i try to work on a new project built around my harness, it feels fragmented, like none of these projects are connected.

in my mind, it only makes sense that every single product built using a harness should somehow be connected: shared judging, a general agent trace surface, etc. the easiest way to do this is generalizing a harness to plug and play with any task you can throw at it.

building a judge pipeline shouldnt require a separate process constantly monitoring runs to hit a compaction checkpoint, and it definitely shouldnt require hooking that monitoring system up for every single type of product we build.

i feel like the initial first step of this is centralizing all services into one shared runtime. this essentially requires a great deslopification, which i am working on right now (11-07-2026).

something that becomes easy as a byproduct of a shared surface is shared memory/knowledge: shared information across coding agents, messaging agents, etc. think about every single product that you use extensively on a day to day basis that incorporates an agent harness of sorts. now, imagine the same product, with all of your product context, knowledge on your past work, your current tasks, etc.

imagine this: an alert posts to the `#alerts` channel in slack. its 8pm on a friday, youre on your way to dinner, still a little on edge from the absurd amounts of stimulants you took 6 hours ago. but its your project, and the issue seems urgent. you rush home, its too late, your lead already saw the alert, and hes pinging you.

now imagine this: it doesnt matter anymore. you have an agent constantly monitoring the alerts channel. it knows what issues youre responsible for, it determines whether a slow query was shipped by your hotfix or someone elses PR.

an alert comes in, its your issue. two messages go out: one to your coding orchestrator, one to your messaging agent. you get a message: 

> "Customer X is facing issue Y in prod. Issue Y was introduced when feature Z was introduced because of A, B, C."

there are so many sources of data that are high signal produced from different sources every single day, and they become so much more valuable when connected.

messaging agents are now able to track peoples locations. what if your coding agent could see whether or not you were close to your computer. it would stop asking questions that you can easily miss.

im definitely going to play around with this idea for the next couple of weeks. see what comes out of it. updates will be posted here.
