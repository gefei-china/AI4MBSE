package '汽车热管理系统活动' {
    doc /* 汽车热管理系统(TMS)活动视图:座舱制冷、电池冷却、除霜除雾三条白盒功能流 */
    private import Actions::Action;
    private import Items::Item;
    private import ScalarValues::*;
    private import StandardViewDefinitions::*;

    package '结构定义' {
        doc /* 执行各动作的内部功能环节(泳道归属方) */

        part def '热管理控制软件TMU';
        part def '空调子系统';
        part def '冷却子系统';
        part def '座舱环境';
        part def '动力电池系统';
        part def '驾驶员';
    }

    package '活动定义' {
        doc /* 三条代表性活动流定义 */
        private import '结构定义'::*;

        action '座舱制冷流' : Action {
            doc /* 座舱制冷:请求-采集-判定-指令-冷媒循环-并行分配-输出-反馈-闭环调整 */

            action '接收制冷请求' : Action {
                doc /* TMU 接收分区温度设定与制冷需求 */
                out '有效制冷请求' : Item;
            }

            action '采集座舱热状态' : Action {
                doc /* 座舱环境采集座舱温度与出风温度 */
                out '当前热状态' : Item;
            }

            action '判定制冷条件' : Action {
                doc /* TMU 综合有效请求、当前热状态与能量状态判定是否允许制冷 */
                in '有效制冷请求' : Item;
                in '当前热状态' : Item;
                out '制冷条件判定' : Item;
            }

            action '反馈受限原因' : Action {
                doc /* 条件不满足时反馈受限原因并异常终止 */
            }

            action '生成制冷控制指令' : Action {
                doc /* TMU 生成压缩机转速、阀开度、风机档位 */
                out '制冷控制指令' : Item;
            }

            action '建立冷媒循环' : Action {
                doc /* 空调子系统执行冷凝散热、节流降压、蒸发吸热 */
                in '制冷控制指令' : Item;
                out '冷媒冷量' : Item;
            }

            action '分配冷量至前排' : Action {
                doc /* 空调子系统向前排蒸发器分配冷量 */
                in '冷媒冷量' : Item;
                out '前排制冷作用' : Item;
            }

            action '分配冷量至后排' : Action {
                doc /* 空调子系统向后排蒸发器分配冷量 */
                in '冷媒冷量' : Item;
                out '后排制冷作用' : Item;
            }

            action '输出座舱制冷' : Action {
                doc /* 座舱环境输出制冷作用,座舱温度下降 */
                in '前排制冷作用' : Item;
                in '后排制冷作用' : Item;
                out '座舱温度' : Real;
            }

            action '采集执行状态' : Action {
                doc /* 空调子系统采集出风温度、风量与压缩机状态 */
                in '座舱温度' : Real;
                out '制冷状态反馈' : Item;
            }

            action '判定目标达成' : Action {
                doc /* TMU 比较当前温度与目标温度 */
                in '制冷状态反馈' : Item;
                out '达成判定' : Item;
            }

            action '保持制冷并终止' : Action {
                doc /* 目标达成,保持当前制冷状态并正常终止 */
            }

            action '调整制冷策略' : Action {
                doc /* 依据未达成偏差修正控制指令,回到生成制冷控制指令形成闭环 */
                out '修正控制指令' : Item;
            }

            // 控制流
            first '接收制冷请求';
            then '采集座舱热状态';
            then '判定制冷条件';
            then decide '制冷条件决策';

            if '判定制冷条件'.'制冷条件判定' == '允许' then '生成制冷控制指令';
            if '判定制冷条件'.'制冷条件判定' == '拒绝' then '反馈受限原因';

            first '生成制冷控制指令';
            then '建立冷媒循环';
            then fork '冷量并行分叉';
            then '分配冷量至前排';
            then '分配冷量至后排';
            then join '冷量并行汇合';
            then '输出座舱制冷';
            then '采集执行状态';
            then '判定目标达成';
            then decide '目标达成决策';

            if '判定目标达成'.'达成判定' == '达成' then '保持制冷并终止';
            if '判定目标达成'.'达成判定' == '未达成' then '调整制冷策略';

            first '调整制冷策略';
            then '生成制冷控制指令';
        }

        action '电池冷却流' : Action {
            doc /* 电池冷却:请求-冷量仲裁-回路指令-冷却液循环-冷量耦合-电池降温-闭环 */

            action '接收电池冷却请求' : Action {
                doc /* 动力电池系统上报电池温度与快冷请求 */
                out '电池冷却请求' : Item;
            }

            action '仲裁冷量优先级' : Action {
                doc /* TMU 在座舱与电池共享冷媒回路时显式分配冷量 */
                in '电池冷却请求' : Item;
                out '冷量分配决策' : Item;
            }

            action '冷量优先分配电池' : Action {
                doc /* 电池优先级高于座舱,冷量优先供给电池冷却 */
                in '冷量分配决策' : Item;
                out '电池优先分配' : Item;
            }

            action '按比例分配并限制座舱' : Action {
                doc /* 电池优先级不高于座舱,按比例分配并限制座舱制冷能力 */
                in '冷量分配决策' : Item;
                out '比例分配' : Item;
            }

            action '生成冷却回路指令' : Action {
                doc /* TMU 生成水泵转速与 Chiller 阀开度 */
                in '电池优先分配' : Item;
                in '比例分配' : Item;
                out '冷却回路指令' : Item;
            }

            action '建立冷却液循环' : Action {
                doc /* 冷却子系统建立冷却液流量与温度 */
                in '冷却回路指令' : Item;
                out '冷却液状态' : Item;
            }

            action '冷量耦合' : Action {
                doc /* 冷却子系统在 Chiller 处由冷媒侧向冷却液侧换热 */
                in '冷却液状态' : Item;
                out '冷却液降温' : Item;
            }

            action '电池降温' : Action {
                doc /* 动力电池系统由冷却液带走热量,电池温度下降 */
                in '冷却液降温' : Item;
                out '电池温度' : Real;
            }

            action '判定温度回落' : Action {
                doc /* TMU 比较电池温度与目标温度 */
                in '电池温度' : Real;
                out '回落判定' : Item;
            }

            action '结束电池冷却' : Action {
                doc /* 电池温度已回落至目标,终止冷却 */
            }

            action '提升水泵转速' : Action {
                doc /* 温度未回落,提升水泵转速后回到建立冷却液循环 */
                out '转速提升指令' : Item;
            }

            // 控制流
            first '接收电池冷却请求';
            then '仲裁冷量优先级';
            then decide '冷量优先级决策';

            if '仲裁冷量优先级'.'冷量分配决策' == '电池优先' then '冷量优先分配电池';
            if '仲裁冷量优先级'.'冷量分配决策' == '按比例' then '按比例分配并限制座舱';

            first '冷量优先分配电池';
            then '生成冷却回路指令';
            first '按比例分配并限制座舱';
            then '生成冷却回路指令';

            first '生成冷却回路指令';
            then '建立冷却液循环';
            then '冷量耦合';
            then '电池降温';
            then '判定温度回落';
            then decide '温度回落决策';

            if '判定温度回落'.'回落判定' == '已回落' then '结束电池冷却';
            if '判定温度回落'.'回落判定' == '未回落' then '提升水泵转速';

            first '提升水泵转速';
            then '建立冷却液循环';
        }

        action '除霜除雾流' : Action {
            doc /* 除霜除雾:请求-吹向切换-出风调温-除霜执行-视野判定-闭环 */

            action '接收除雾请求' : Action {
                doc /* 驾驶员请求或起雾状态触发 */
                out '除雾请求' : Item;
            }

            action '切换吹向模式' : Action {
                doc /* TMU 切换吹向除霜并提升风量 */
                in '除雾请求' : Item;
                out '吹向除霜模式' : Item;
            }

            action '调节出风温度' : Action {
                doc /* TMU 与空调子系统协同将出风温度调至目标值 */
                in '吹向除霜模式' : Item;
                out '目标出风温度' : Item;
            }

            action '除霜执行' : Action {
                doc /* 座舱环境以热风吹拂挡风玻璃,视野恢复 */
                in '目标出风温度' : Item;
                out '视野恢复情况' : Item;
            }

            action '判定视野状态' : Action {
                doc /* TMU 判定挡风玻璃视野是否恢复 */
                in '视野恢复情况' : Item;
                out '视野判定' : Item;
            }

            action '恢复原吹向模式' : Action {
                doc /* 视野恢复,恢复原吹向模式并终止 */
            }

            action '提升风量' : Action {
                doc /* 视野未恢复,提升风量后回到除霜执行 */
                out '风量提升指令' : Item;
            }

            // 控制流
            first '接收除雾请求';
            then '切换吹向模式';
            then '调节出风温度';
            then '除霜执行';
            then '判定视野状态';
            then decide '视野状态决策';

            if '判定视野状态'.'视野判定' == '完成' then '恢复原吹向模式';
            if '判定视野状态'.'视野判定' == '继续' then '提升风量';

            first '提升风量';
            then '除霜执行';
        }
    }

    package '视图定义' {
        doc /* 活动视图投影 */
        private import '活动定义'::*;

        view '热管理系统活动视图' : ActionFlowView {
            expose '座舱制冷流';
            expose '电池冷却流';
            expose '除霜除雾流';
        }
    }
}
