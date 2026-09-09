# parsnip-node
A battery-powered, portable Meshtastic/Meshcore node built around the ESP32-S3, an Ebyte E22P-915M30S LoRa front end, and a u-blox NEO-M9N GNSS receiver. Designed in KiCad, it targets 915 MHz operation in Canada under ISED RSS-247.

## Overview

The goal is a self-contained LoRa node that pairs one of the longest range, best value LoRa modules, with an accurate GNSS reciever, running off two 21700 cells in series, to connect to other people on the LoRa mesh network community.

### Schematic - v2

<table>
  <tr>
    <td align="center" width="33%">
      <a href="lib/img/schematic-gnss.png"><img src="lib/img/schematic-gnss.png" alt="GNSS / active antenna schematic" width="300"></a>
      <br><sub><b>GNSS / active antenna</b></sub>
    </td>
    <td align="center" width="33%">
      <a href="lib/img/schematic-lora.png"><img src="lib/img/schematic-lora.png" alt="LoRa front end schematic" width="300"></a>
      <br><sub><b>LoRa front end</b></sub>
    </td>
    <td align="center" width="33%">
      <a href="lib/img/schematic-rails.png"><img src="lib/img/schematic-rails.png" alt="Power rails schematic" width="300"></a>
      <br><sub><b>Power rails</b></sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="33%">
      <a href="lib/img/schematic-charger.png"><img src="lib/img/schematic-charger.png" alt="Charger and BMS schematic" width="300"></a>
      <br><sub><b>Charger / BMS</b></sub>
    </td>
    <td align="center" width="33%">
      <a href="lib/img/schematic-usb.png"><img src="lib/img/schematic-usb.png" alt="USB-C / PD schematic" width="300"></a>
      <br><sub><b>USB-C / PD</b></sub>
    </td>
    <td align="center" width="33%">
      <a href="lib/img/schematic-peripherals.png"><img src="lib/img/schematic-peripherals.png" alt="Peripherals schematic" width="300"></a>
      <br><sub><b>Peripherals</b></sub>
    </td>
  </tr>
</table>

### PCB - v2

<p align="center">
  <img src="lib/img/pcb-layout.png" alt="parsnip-node PCB layout, all layers" width="330">
</p>

### 3D Render - v2

<p align="center">
  <img src="lib/img/3d-render.png" alt="parsnip-node board 3D render, front" width="330">
</p>

### In the field - v1

<table>
  <tr>
    <td align="center" width="33%">
      <img src="docs/img/pcb-front-table.png" alt="Assembled parsnip-node board on the bench" width="260">
      <br><sub><b>Assembled board</b></sub>
    </td>
    <td align="center" width="33%">
      <img src="docs/img/pcb-outside.png" alt="parsnip-node during outdoor testing, front" width="260">
      <br><sub><b>Outdoor testing</b></sub>
    </td>
    <td align="center" width="33%">
      <img src="docs/img/pcb-outside-back.png" alt="parsnip-node during outdoor testing, back" width="260">
      <br><sub><b>Outdoor testing, back</b></sub>
    </td>
  </tr>
</table>

## Hardware
 
| Part | Price |
| --- | --- |
| JLCPCB PCB w/ Assembly (to-be changed [BOM](production/bom.csv)) | $1098.04 CAD (est) |
| 21700 Cells [18650batterystore.com](https://www.18650batterystore.com/products/samsung-58e-21700-battery)| $40 CAD  |
| 3.7" E-Ink [AliExpress](https://www.aliexpress.com/item/1005009712001279.html?mp=1)| 24$ CAD |
 
### Board
 
- 4-layer with ENIG, 60 x 130 mm
- JLCPCB `JLC04161H-7628` stackup, 2 oz copper outer, 2 oz copper inner
- L1-L2 prepreg 0.2104 mm, Dk 4.4
- RF: 0.36mm trace width for ~50 Ω CPWG on this stackup, ground rails stitched with 0.3 mm / 0.6 mm vias at roughly 1.5 to 2 mm pitch
  
### ESP32-S3 GPIO pin map

Host: ESP32-S3-WROOM-1-N16R8 (U1). GPIO numbers are ESP32-S3 GPIO, not module pins.

#### LoRa - E22P-915M30S (U12)

| Signal | GPIO |
| --- | --- |
| NSS | 10 |
| SCK | 12 |
| MOSI | 11 |
| MISO | 13 |
| BUSY | 14 |
| DIO1 | 4 |
| NRST | 40 |
| EN | 41 |

#### GNSS - NEO-M9N (U9)

| Signal | GPIO |
| --- | --- |
| TXD | 1 |
| RXD | 2 |
| TIMEPULSE | 3 |
| EXTINT | 48 |
| SDA | 8 |
| SCL | 9 |

#### Other peripherals

| Function | GPIO |
| --- | --- |
| I2C SDA | 8 |
| I2C SCL | 9 |
| I2C IRQ | 7 |
| E-Ink BUSY | 5 |
| E-Ink RESET | 6 |
| E-Ink D/C | 18 |
| E-Ink CS | 17 |
| Bus CS | 38 |
| USB D- / D+ | 19 / 20 |
| UART0 TX / RX | 43 / 44 |
| RTC 32 kHz xtal | 15 / 16 |
| BOOT | 0 |
| eFuse PG | 39 / 42 |
| EMI osc gate | 47 |
| Level shifter | 46 |
| Load switch | 21 |
| PSRAM (reserved) | 35 / 36 / 37 |
| VDD_SPI (reserved) | 45 |
 
## Firmware
 
Firmware is a Meshtastic fork tracked as the `firmware` submodule ([LimesKey/firmware](https://github.com/LimesKey/firmware)). Development uses VS Code with the pioarduino extension, which Meshtastic pins for ESP32-C6 / Arduino-ESP32 3.x support. A device variant declares the pin map above and the PA control lines so TX actually keys up. Unfortunately there are some extensive changes to the Meshtastic firmware in order to incorporate a SPI GNSS module, as this is not natively supported by Meshtastic.
 
```bash
git clone --recurse-submodules https://github.com/LimesKey/parsnip-node.git
```