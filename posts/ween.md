---
title: things i've built with my harness
date: 10/26/2026
tags: [engineering, harnesses]
---

one of the nicest things about having your own harness is the surface of application, especially in a world where every company is essentially building "harnesses for X/Y/Z"

while these companies are distributing and hardening these harnesses to be scaled to thousands (or even millions) of daily active users, rebuilding parts of their project i find interesting only has to serve me.

a very particular instance of this is messaging services, like instinct, which are at its core, just forks of other traditional, very strong performing harnesses (like pi, hermes, etc) but the engineering process is so much more extensive because they HAVE to care about infra + scaling.

if i wanted to build a texting agent, i wouldn't have to care about scale, or computer, or storage limits. i don't need to worry about whether or not shipping a new feature could quadruple the llm bill. privacy isn't a huge concern either because i'm very close with the only user of the product: me.

![lol](/images/ofc.webp)

the great thing about this era of software engineering is that 99% of companies that i find very cool mostly revolve around building really really good harnesses for a specific task.

whenever i have a free weekend, i try to spend a couple hours throwing together a similar product, on top of my own harness, and just have it live on my home lab.

## messaging agents
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
