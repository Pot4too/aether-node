PMOS protection we have now has a fatal issue when USB power is in (5V) the inner PMOS body diode will flow to source (battery) from drain (LDO_IN) causing uncontrolled, unwanted battery charging

one option is to add another diode between source and battery so it only allows flow to source. But that has an issue of 0.45V drop on the diode so our 3.7V battery voltage becomes 3.3V which is fairly low.

In theory maximum minimal tolerable volatge IN for LDO is 3.2V - 3.3V, if we assume 3.7V on battery - 0.45V we are around 3.25V most of the time which is not ideal.

This is very weird. When we have it in typycal connection D to LDO_IN, S to Batter and Gate on USB (5V) we get leakage basicly all our current goes to battery

Buuut when we swap D and S, so S is LDO_IN, D is Battery and Gate is again USB (5V) it behaves as it should, and also it works when USB is 0V. I am fucked by this solution and dont understand it.

thinking about addin some 1uA load to battery as constatnt that even if we have 3nA leakage via PMOS its absorbed by this constant load thus protecting battery from overcharging ?

consultation with claude: https://claude.ai/share/8fd812b1-f4a6-42e3-82d6-3930dad1a98f
