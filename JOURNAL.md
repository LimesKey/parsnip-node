## Starting the journal - 4 hours (2026-08-07)

I thought I should probably get into some more Hack Club stuff, and add more documentation for what I'm doing so I don't forget. I'm in the process of revamping the circuit protection. For the amount of money these modules and ICs cost (~$50 CAD), I think it's wise to throw at least $5 in circuit protection components at it. Of course the best circuit protection is just being careful and taking the proper precautions, but I'm a little silly.

I've already protected the RF frontends of the LoRa and GNSS modules, with ESD diodes and current limiting, so I'm moving on to the power inputs. I'm looking at implementing the `TI TPS25947` family of eFuses, specifically the [TPS259470LRPWR](https://www.ti.com/product/TPS25947/part-details/TPS259470LRPWR), which Claude suggested. This eFuse has both an overvoltage protection feature and an overcurrent protection feature. In my GNSS module's datasheet, the NEO-M9N has a maximum power supply voltage of 3.6 V. I can then set the eFuse's OVLO (overvoltage lockout) to cut off power when the input voltage exceeds 3.55 V, just below the NEO's maximum. However, there is one issue with this. In order for the eFuse to recover and continue operation after a fault state, the 3V3 input power rail must fall below 3.25 V, which would never happen in regular use. But that would just mean I have to power off the device and turn it on again, which isn't that bad considering it protected my expensive module from frying.

![KiCad Schematic TPS259470](/docs/img/Screenshot_1-2026_08_07_23:45.png)

Considering maybe adding a cheap 50-cent TVS diode to the TPS259470LRPWR input, as the datasheet says it may be helpful. I'll have to see.

## Finalizing circuit protection - 2 hours (2026-08-08)

* Switched from the `TPS259470LRPWR` to the `TPS259474LRPWR` for slightly better OVLO tolerances (5.5% to 1.7%), allowing the trip voltage to be more precise.
* Added a rechargeable battery to V_BCKP on U2.
* Decided on using the `TPS259474ARPWR` for the future buck converter output.

![KiCad Schematic GNSS](/docs/img/Screenshot_1-2026_08_08_03:13.png)

## Research BMS and charge controller choices - 2 hours (2026-08-09)

I think I've settled on using the `MAX17320Gxx`. I was thinking of initially going with the `BQ40Z50`, but with that one you are forced to use some of TI's software and buy their testing equipment in order to program it. The MAX17320 is only a little more and has a whole host of nice-to-have features like cycle count and intelligent algorithms to calculate percentage. It is however quite expensive at $8 CAD per IC, but I think it's worth the safety and ease of use aspects. I watched [this YouTuber](https://www.youtube.com/watch?v=UUr-CJudg38) on how he designed his BMS, it was quite insightful.

![MAX17320 schematic image](/docs/img/MAX17320.png)

### Charge Controller Selection

As for the charge controller, I went with the [BQ25798](https://www.ti.com/product/BQ25798). It's relatively cheap at $3 CAD each, it's modern and a recent design, and highly efficient. It supports a wide range of input voltages (USB-C PD fast-charging!) and can even communicate with the microcontroller to set custom charge limits to preserve battery health. However, it's a little noisy with its four switching MOSFETs, so it's going to take some consideration in layout so as not to interrupt any data lines and RF components/traces.

My goal was to get a slightly more premium BMS that had some of the nice features you'd see in mobile phones.

## Wiring up the BMS - 3 hours (2026-08-10)

| my design | reference design |
| :---: | :---: |
| <img src="docs/img/bms-wiring.png" width="800" alt="Custom Schematic"> | <img src="docs/img/max17320-schematic-examp.png" width="800" alt="Reference Schematic"> |

I'm sure there are still some issues or things to double check, but it's a good start. I studied the example block diagram schematic from the datasheet, and a few other schematics I could find when googling "MAX17320G22+ kicad schematic". Claude said the `AON7534` N-channel MOSFET would be best, so I trusted it. I'm trying to think about how I'll have the thermistor placed, because I'm using two 21700 battery holders next to each other. Perhaps I could expose some copper and use one of those SMD NTC thermistors? The batteries would then be sitting very close to the thermistor, and perhaps I could add some type of thermally conductive but not electrically conductive material or foam. I don't like the idea of taping the thermistor to the batteries...

## Confirming BMS wiring, adding testpoints and voltage/power ratings to components - 3 hours (2026-08-13)

Spent some time today double-checking the wiring on the BMS and learning more about it from the datasheet. I'm much more confident in my wiring now, and I added some DNP components in case I need to swap anything or make changes to it afterwards, plus testpoints as well. Most of the capacitors I was planning on using are well below the voltage rating they should be for the BMS, so I made sure to explicitly mention capacitor voltage ratings in the part field in KiCad.

Next steps would be to make the `MAX17320G22+` symbol a little prettier and organize my wiring, and to wire the charge controller. It's a little hard to read in its current state. So many components on such a small symbol. Maybe I'll ask in the KiCad Discord for suggestions on how to make the BMS schematic a little clearer.

![BMS MAX17320 changes](/docs/img/bms-revamp.png)

## Wiring up the charge controller & selecting buck converter and PD IC - 4 hours (2026-08-14)

Following the reference application schematic for the [BQ25798](https://www.ti.com/product/BQ25798) and calculated the inductor current for my application. Reading over the data, it's actually a pretty impressive piece of silicon for how many features it has in such a small package. It uses narrow voltage DC (NVDC), so it'll take power directly from your USB-C circuitry, regulate it within a certain range similar to your battery, and then you can power your buck converter and downstream devices from its `SYS` output at up to 6 A. No external power muxing needed, and it's really efficient. Battery charge current can be set fixed, or dynamically through its I2C bus. The downside is the high frequency switching (750 kHz - 1.5 MHz), but I think if I physically keep it distanced far enough away from the RF components, and choose a good inductor, I should be okay.

One thing I tried to keep in mind for the charge controller and BMS circuitry: even though I'm only planning on having two series batteries, I should try to make most of the components compatible with the maximum 4S that both ICs support. This means having all the components rated for `>25V`, so choosing capacitors that are rated for 50 V. In turn this would mean having to add more capacitors than I normally would, in order to satisfy capacitor DC bias derating at higher voltages.

![wired BQ25798 schematic](/docs/img/bq25798.png)
*I do need to clean this up and make it look less muddled...*

For the USB-PD, I wanted to choose something cheap, easy and small. I have no need for extra features or for it to support a wide range of USB chargers. I went with the [HUSB238](https://en.hynetek.com/2421.html), specifically the `HUSB238_002DD`. It's about $0.70 CAD each on LCSC, and supports a fixed 9 V USB trigger. The more common option would be to use the `CH224K`, but it had a large package and looked "old", although it's cheaper.

Lastly, I need a 5 V rail to run off the outputted `SYS` from the charge controller, so approximately a `5V - 8.8V` input. Claude helped me choose the `LM61460-Q1`, specifically the [LM61460AASQRJRRQ1](https://www.ti.com/product/LM61460-Q1/part-details/LM61460AASQRJRRQ1). It's a little steep at $2 CAD on [LCSC](https://www.lcsc.com/product-detail/C1855832.html), but using TI components is nice, and this IC is ideal for powering sensitive high-frequency components (it has "Low EMI" in the title). One of the ways it achieves this low EMI is by letting you customize its switching frequency, thereby sacrificing efficiency for switching at a frequency that doesn't interfere with frequencies elsewhere on the board.

> The switching frequency can be set or synchronized between 200 kHz and 2.2 MHz to avoid noise sensitive frequency bands.

## Reworking power rails, adding antenna detection, and organization - 6 hours (2026-08-18)

* Added the [LM61460-Q1](https://www.ti.com/product/LM61460-Q1/part-details/LM61460AASQRJRRQ1) as my 3V3 switching buck converter, which is the same as for the 5V rail.
 
* Added an eFuse to the output of my 5V buck for additonal protection

* Added LoRa antenna detection to prevent high power transmits from damaging the E22P RF frontend.

* Renamed schematic sheets for clarity and to be updated for the name change from `limeskey-node` to `parsnip-node`
  
* Double checked capacitor voltage ratings are correct for the net

For the antenna detection, I didn't think it was possible to implement without something like an expensive VNA. But apparently, since most LoRa antennas are DC-shorted, you can put a small amount of current on the antenna trace and check and see if it's still there with a FET. If the antenna is installed correctly, there should be no DC voltage on the antenna net, but if it wasn't, it would show 3V3 and the LoRA module would automatically stop transmitting.

![5v rail](/docs/img/5v-rail.png)

## USB Interface - USB-OTG at 60W! - 6 hours (2026-08-21)

### USB-OTG
I learnt what USB-OTG was and it turns out my charge controller already supports it, and for two dollars more, I could upgrade my current USB-PD IC to something that also supports USB-OTG. USB-OTG allows USB device to charge another device, acting like a power supply, with the same voltages and currents a regular wall-adapter USB-C brick would do. 

The `BQ25798` can do USB-OTG at 3.32A maximum, and at up to 22V; I'll likely set it to do 20V at 3A. My 2S 21700 batteries can do approximately 10A contiously at 8V, so something around 80W total, but it's best not to push them that far. And 60W, what the maximum is for the BQ25798, is plenty. 

### USB Power Delivery
As for USB-PD, I went from the $0.50 USD `HUSB238` to the $2.50 USD `TPS25751`, specifically for the USB-OTG feature. It also turns out this new PD IC has some nice circuit protection features, like reverse current protection, undervoltage and overvoltage protection, protection against some non-compliant USB devices for the CC pins, but most importantly for me, it has liquid detection. It can detect when water gets into the USB-C port and either slow down charging, or shut off completely. You do need four FETs in order to use this feature, but they aren't very expensive or large.

Additionally I added some optional protection components to the USB-C port, a surge protection device [TVS2200DRVR](https://www.ti.com/product/TVS2200/part-details/TVS2200DRVR), a TVS device [TPD4E05U06](https://www.ti.com/product/TPD4E05U06/part-details/TPD4E05U06DQAR), and an ESD protection device [TPD8S300A](https://www.ti.com/product/TPD8S300A). These were a little expensive (a few dollars), and a little large, but they're really nice to have. I want my board to withstand anthing basically lol.

![USB Interface](/docs/img/usb-pd.png)

Next steps would probably be to double check I wired everything right, then move onto wiring the ESP32-S3 which hopefully won't be very difficult.

## E-Ink Display & Schematic Cleanup - 4 hours (2026-08-24)

### Cleanup
Today I worked on making the schematic a bit easier to read and understand, as I'm having a few people review it. Additionally, I assigned communication labels to a few unused pins on my ESP32 and tidied up the connector section in the root schematic. I am thinking it would be helpful if I were to expand the size of some symbols so it's easier to see all the components in between the IC pins. I have plenty of schematic room so why not make everything a little larger and more spread out? It's what I commonly see in more professional schematics, such as my laptop's motherboard schematic.

### E-Ink
I am toying with the idea of adding a 2.9" E-Ink/E-Paper display ([AliExpress](https://www.aliexpress.com/item/1005004644515880.html)) on the back of the PCB, against where the two 21700 batteries go. I'm thinking it could be attached to long M.2 standoffs ([AliExpress](https://www.aliexpress.com/item/1005008713639234.html)), and screwed into my board's mounting holes. It would be nice then to show information about where the user is currently located and how many LoRa nodes the device currently sees. It seems like non-tech people that I show my prototype/v1 device to, they are offput by the idea of having to use a phone. Perhaps they feel like it defeats the purpose of my device. To me, it doesn't impact the device much at all, since I'm only simply using the display and touchscreen of the phone and that's all. The phone doesn't have to have any sort of GNSS/Wifi/Cellular Internet for it to work, but non-techy people don't really understand that.

However, if I were to add a display, it would be intuitive for the user to also interact with the display. I would almost have to add a button of some sorts. I was considering adding a [capacitive sensor](https://hackaday.io/project/202684-io-touch-every-io-pin-is-a-capacitive-sensor) to my PCB, it looks pretty simple, however it requires a lot of space, space which I don't really have. Need to think more...

Oh, also, I changed from a `GPL-3.0` license to the `CC BY-NC-SA 4.0` license, it better aligns with my views.

![ESP32 Wiring, root schematic](/docs/img/esp32-root.png)

## Reducing Buck Converter EMI - 6 hours (2026-08-26)

As it stands, I have two of the same low-EMI buck converter ICs for my 5V and 3.3V rail. Both of them switch at the same frequency, which should be fine, right? Wrong, both buck converters have drift and may not statup at the exact same time; and even if they were in sync/phase, you wouldn't want them to be anyways. Ideally, you'd want your buck converters to be switching at alternate times, one switches after another. Fortunately, there is a sync feature built right into the buck converters which can do exactly this. However you'd need an external clock.., well maybe two, one 0 degrees phase shifted, and another 180 degrees phase shifted.

Well, you could use a [4MHz oscillator](https://www.lcsc.com/product-detail/C2901612.html), and a [flip-flop](https://www.ti.com/product/SN74LVC1G74), sounds like a plan. Until you realize the oscillator draws 10 mA!!!! I don't have anything on the board except the main buck converter itself that could provide 3V3 @ 10mA. And of course, you can't power the oscillator from the output of the very buck converter it's trying to switch on, right? Actually apparently you can. The sync pin is very much optional, and when it's absent, the buck converter relies on it's own oscillator. So basically the only downside is poor EMI for 1 second at startup, which is nothing.

![Buck oscillator circuit](/docs/img/oscillator-buck.png)

Also in this journal:
* Adjustments to eFuse limits again
* Reduce 5V buck from 5.1v to 5.02v for added safety
* Add footprints & voltage ratngs to a lot of capacitors
* Fix flipped FET for liquid detection
* Add a magnometer

## Starting PCB Layout - 4 Hours (2026-08-29)

Worked today on the PCB, finally moving on from the schematic. Hopefully I can get it fully wired within the next few days. I am starting with grouping together ICs with their components as per the schematic, and thinking about where all the major components and items should go. Connectors on the bottom, battery on the backside top, the BMS & battery circuitry middle-left, and the buck converters middle-right.

![PCB View](/docs/img/pcb-layout.png)

## Layout done - working on routing - 4 hours (2026-09-01)

Went to a local makerspace, Hacklab, to work on the PCB some more. Now that I'm on my laptop, I didn't have the 3D Models transfer over, since I was using easyeda2kicad and that put the models inside a local directory on my computer - not in the repo. I re-downloaded all the 3D Models and moved them into `lib/3d-models` and then changed the 3D model location for all missing components. Now when someone opens my PCB and checks the 3D View, all the components should show up nicely. A couple parts had incorrect footprints for the part, and I was able to fix this after seeing the 3D Model was obviously different from the footprint. That's one reason I like 3D Models so much.

## Routing done - 8 hours (2026-09-07)

Spent the last several days routing, it has been pretty awful. I don't know why I thought the routing would be easy to complete in a few days if the schematic took a month and a half to do. It would have been so much nicer if the board was larger. Difficult parts were ensuring my traces were wide enough, there's so many different nets all requiring 3A+,then routing the communication lines & USB data pairs. There's so many communication lines going all over the board, it's atrocious. I'm so glad to be mostly finished.

Credit to @spheresva for helping a bit with layout.

![PCB KiCad Routing](/docs/img/routing-pcb.png)
