#BSD 3-Clause License
#
#Copyright (c) 2023, ASU-VDA-Lab
#
#Redistribution and use in source and binary forms, with or without
#modification, are permitted provided that the following conditions are met:
#
#1. Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
#2. Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
#3. Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
#THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import openroad as ord
from openroad import Tech, Design, Timing
import os, odb, drt
from demo1_helpers import load_design
from pathlib import Path
import argparse

parser = argparse.ArgumentParser(description="Path to root of the tutorial directory")
parser.add_argument("--path", type = Path, default='./', action = 'store')
pyargs = parser.parse_args()
tech, design = load_design(pyargs.path, verilog = False) 
# tech, design = load_design(Path("../", verilog = False) # For demo to be copied.

timing = Timing(design)
corner = timing.getCorners()[0]
block = design.getBlock()

############
#cell query#
############
print(" name      | library_type | dynamic_power | static_power")
insts = block.getInsts()[-10:]
for inst in insts:
  inst_static_power = timing.staticPower(inst, corner)
  inst_dynamic_power = timing.dynamicPower(inst, corner)
  inst_name = inst.getName()
  libcell_name = inst.getMaster().getName()
  print(f"{inst_name:<11}| {libcell_name:<13}| {inst_dynamic_power:14.4e}| {inst_static_power:12.4e}")
  #hit tab for all available apis (ex. inst.[tab])
  #the return type of power is float!!!
print(f"Power return type: {type(inst_static_power)}")
print("###################################################################")
###########
#net query#
###########
print(" name       | net_type | pin&wire_capacitance")
nets = block.getNets()[:10]
for net in nets:
  pin_and_wire_cap = timing.getNetCap(net, corner, timing.Max)
  net_name = net.getName()
  net_type = net.getSigType()
  print(f"{net_name:<12}| {net_type:<9}| {pin_and_wire_cap:19.4e}")
  #hit tab for all available apis (ex. net.[tab])
  #the return type of pin_and_wire_cap is float!!!
print("###################################################################")
###########
#pin query#
###########
print(" name        | rise_arrival_time | fall_arrival_time | rise_slack | fall_slack | slew")
for inst in insts:
  inst_ITerms = inst.getITerms()
  for pin in inst_ITerms:
    if design.isInSupply(pin):
        continue
    pin_name = design.getITermName(pin)
    pin_rise_arr = timing.getPinArrival(pin, timing.Rise)
    pin_fall_arr = timing.getPinArrival(pin, timing.Fall)
    pin_rise_slack = timing.getPinSlack(pin, timing.Fall, timing.Max)
    pin_fall_slack = timing.getPinSlack(pin, timing.Rise, timing.Max)
    pin_slew = timing.getPinSlew(pin)
    print(f"{pin_name:<12} | {pin_rise_arr:17.4e} | {pin_fall_arr:17.4e} | {pin_rise_slack:10.4e} | {pin_fall_slack:10.4e} | {pin_slew:6.4e}") 
    #hit tab for all available apis (ex. pin.[tab])
    #the return type of slack is float!!!
    #timing-related properties go through timing.[tab] apis
print("###################################################################")
print(" name          | rise_arrival_time | fall_arrival_time | rise_slack | fall_slack | slew")
for net in nets:
  net_ITerms = net.getITerms()
  for pin in net_ITerms:
    pin_name = design.getITermName(pin)
    pin_rise_arr = timing.getPinArrival(pin, timing.Rise)
    pin_fall_arr = timing.getPinArrival(pin, timing.Fall)
    pin_rise_slack = timing.getPinSlack(pin, timing.Fall, timing.Max)
    pin_fall_slack = timing.getPinSlack(pin, timing.Rise, timing.Max)
    pin_slew = timing.getPinSlew(pin)    
    print(f"{pin_name:<14} | {pin_rise_arr:17.4e} | {pin_fall_arr:<17.4e} | {pin_rise_slack:10.4e} | {pin_fall_slack:10.4e} | {pin_slew:6.4e}")
print("###################################################################")


