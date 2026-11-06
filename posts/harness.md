---
title: harness engineering
date: 10/21/2026
tags: engineering
---

> preface: this is going to be a long running article about harness engineering. i will update this post continuously throughout the future

i've been working on my own harness for a while. out of everything i've built, a 0 -> 1 harness has been the most rewarding project i've worked on, and also the most frustrating to get right. it took me a long time to get it to a point where i can use my own harness as a daily driver for everyday work. 

one of the greatest things about building your own harness is extensibility. you can build any tool, any micro-interaction can be fine tuned to your expectations, you can implement the most modern compaction mechanisms... yada yada yada.

how do you know when to stop?

## my first agent "harness"
a couple months before i built my new harness, i started getting more into the dangerous realm of the agentic side of twitter. i had a general idea of a workflow that i enjoyed, which eventually evolved to become something like an orchestrator -> subagent workflow. managing these workflows in claude code and codex separately was hard. claude loved subagents, but would kill my max plan in like ~20 minutes; codex hated subagents and refused to spawn any unless i specifically asked for them.

so i built my own little thing. in the moment, i thought i was the greatest to ever do it. looking back now, i'm pretty sure i was in a state most people would identify as ai psychosis. 

it was a gui written in rust, which started an mcp server on launch with tools to do a very minimal set of things: spawning subagents, sending messages between agents, etc. it also happened to be like 80k lines of code...

i used it as my daily driver for a good while. it worked, but not super well. every issue that i had was immediately resolved by forward fixes that introduced 10 more nits that i didn't like. scope creeps amirite.

so, i scrapped it after 2 months, rethought what i really needed with a daily driver, and started working on my own new thing.

## motivation
at the time, i was heavily inspired by the engineering paradigms that motivated the creation of [pi](https://github.com/earendil-works/pi). i loved the idea that a harness with 4 simple tools could be extended to be a daily driver.

i considered simply forking pi, adding some tools and extensions and calling it a day. ultimately, i decided against it primarily for the reason that i wanted to learn, and forking isn't learning. 

the current state of agent harnesses oddly has a lot of similarities to linux distributions: claude code & codex are ubuntu, pi is arch linux, the rest of them are.... somewhere in between the two.

looking back, i never learnt as much about computers, and programming, as i did when i spent hundreds of hours setting up and using arch linux as my daily driver. the lessons from that never really left me: i'm still a neovim fanatic, i love tmux till this day, and nearly 99% of my everyday work is done through terminal interfaces. 

## a harness in a haystack of harnesses
after about 90k LOC, here is the current state of my harness. 

### standard tooling
* `bash`: execute bash commands
* `edit`: edit a file
* `write`: write a new file
* `read`: read a file
* `load_skill`: load skills
* `fetch`: fetch contents from url
* `websearch`: natural language web search
* `todo`: crud tool for agent managed todo lists
* background tasks: `task_input`, `task_output`, `run_background`
* mcp support

### generic subagent tooling
* `spawn`: spawn subagents
* `cancel`: cancel subagent run
* `output`: get subagent output
* `send`: send message to active subagent
* `status`: get the status of a subagent

subagents also have profiles, which specify tool scope (investigation subagents don't get write tools, etc), handoff prompt, default models, etc. this allows me to turn a workflow into something reusable overtime. 

### attention
i like not being in the loop all the time. i prefer my agents to do a lot of work autonomously. i don't like having to answer obvious questions, and having to constantly worry about projects being blocked on user decisions.

something that i've found extremely helpful is allowing agents to decide whether or not a question truly needs a human decision. if so, they can ask me questions through `request_attention` which i can then check with `/decisions`.

![decision example](/images/decisions.png)

it's especially nice since agents can defer blocking decisions until i answer, and continue working on the unblocked paths. 

if i need more context associated with a decision, its configured so that i can chat with a fork of the orchestrator run from the point where it asked me about the decision, so i don't have to interrupt the main orchestrator.

### projects
as a natural consequence of the adoption of orchestrator workflows, it made sense to link each project to an active orchestrator run.

while this puts restrictions on having multiple simultaneous orchestrators for one project, i've found that this is a limitation that can be easily accepted, especially since i never need more than one orchestrator working on a project at a time.

i've built most of my workflows around this known constraint, but it has opened a couple of important doors for me, most importantly: each project can have a unique system prompt associated with it, so that i can specify different workflows for different projects.

the base system prompt never changes, but its short and simple: introducing tool use, speaking tone/structure, and identity. project specific system prompts are loaded on top of this base system prompt.

i'm aware the conventional way to do this is through an `AGENTS.md` file in the project directory, but working on a multi-contributor project makes it difficult to use `AGENTS.md` for **my** specific workflows. in my mental model, `AGENTS.md` is used for more project specific guidance, not for workflow specific instructions. 

i've found that project specific system prompts have worked very well for me. i work through/experiment with dozens of different ideas a week, and an idea that i'm trying to one shot/prototype with **needs** vastly different instructions in comparison to a project that i'm heavily engaged in and constantly giving new ideas/inputs to.

furthermore, not all projects that i enjoy working on are engineering related. for example, i have a research project that formats research in a specific way that i can digest easily: videos, interactive HTML pages, etc. ultimately, its just easier for me to apply my different intrinsic workflows across different types of projects.

as a natural extension of this, its nice to be able to have orchestrators for different projects talk to each other. i almost always have orchestrators running on my work laptop: one for work, one for research, and one for work on the harness. i've taught my agents to be pretty seamless at communicating with each other: research questions go to the research orchestrator, issues with my agent harness to the harness specific orchestrator, and it autonomously validates and patches the bug.

## things i've built
i've done so many things with my harness. i built my own messaging agent by extending it to give it tools to send messages using linq. now it sends me a daily digest in the morning before i go to work.

i named the messaging agent `ween`, it runs on my old laptop at home, active 24/7. its especially nice to be able to text it to ask for progress updates on my other orchestrators when i'm outside dilly dallying, which it does through `ssh` and `tmux` (so useful right!?) 

one of the nicest things about having your own harness is the surface of application, especially in a world where every company is essentially building "harnesses for X/Y/Z"

while these companies are distributing and hardening these harnesses to be scaled to thousands (or even millions) of daily active users, rebuilding parts of their project i find interesting only has to serve me.

a very particular instance of this is messaging services, like instinct, which are at its core, just forks of other traditional, very strong performing harnesses (like pi, hermes, etc) but the engineering process is so much more extensive because they HAVE to care about infra + scaling.

if i wanted to build a texting agent, i wouldn't have to care about scale, or computer, or storage limits. i don't need to worry about whether or not shipping a new feature could quadruple the llm bill. privacy isn't a huge concern either because i'm very close with the only user of the product: me.

![lol](/images/ofc.webp)

the great thing about this era of software engineering is that 99% of companies that i find very cool mostly revolve around building really really good harnesses for a specific task.

whenever i have a free weekend, i try to spend a couple hours throwing together a similar product, on top of my own harness, and just have it live on my home lab.

### messaging agents
> ween: a personal messaging agent, built on top of my own harness.

traditionally, i am not a big fan of messaging agents. it comes down to two things:
1. theyre too chatty
2. they lack user context

i dont always have the energy to tell instinct that the flight ticket that i purchased under my name, sent to my email, was in fact me and not someone else.

a little bit ago, i found this messaging agent called kat, which i played around with by getting it to send me papers about the latest compaction/memory algorithms in harness engineering every couple days.

it was really really good at this, and i think using kat allowed me to realize how valuable messaging agents could be if you could just strip down the noise.

### setting it up
this was surprisingly a lot easier than i thought.

[link](https://linqapp.com/) provides free numbers, so setting up a stock messaging agent just requires giving the agent a tool to text you + setting up a webhook for events on inbound texts.

thats it. it took me 15 minutes of set up (which was mostly opus) and i could just text an agent running on my laptop at home.

however, building a smoother messaging agent requires some work.

### personalization
at the end of the day, messaging agents fall short (imo) because theyre shipping one generic agent, with the ability to "learn" and adapt.

personalization becomes crazy simple if youre building for yourself. 

1. creating a decent persona becomes stupid easy: "talk like this", "do X if Y happens, otherwise do Z"
2. **automation becomes simple**
   * checking in on my agents while out and about is simple
   * i dont even think about organizing my calendar for the week (or the month)
   * reviewing agent traces and iterating on the harness happens instantaneously

every night, my main work laptop sends agent traces to my personal laptop. i've set up very strict local judge to autonomously iterate, and improve failure points within my hours of usage during the day.

when i wake up, i get a couple of messages:
* bug X (which i don't remember encountering) was fixed
* thing Y happened overnight
* person Z emailed you, heres a drafted response

there is so much more to come. i need to find a better way to organize these blogs specifically about harness engineering.
