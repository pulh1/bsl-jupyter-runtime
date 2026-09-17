&После("ПриНачалеРаботыСистемы")
Процедура OnecInteractiveRuntime_ПриНачалеРаботыСистемы()
	ИдентификаторПродуктаRuntime = "onec-interactive-runtime";
	ВерсияАртефактаRuntime = "0.1.6";
	ВерсияПротоколаRuntime = "3";
	ПродолжатьЦикл = Ложь;
	С = 1; // @runtime-extension-service-breakpoint
	Если ПродолжатьЦикл Тогда
		RuntimeKernelServer.Запустить();
	КонецЕсли;
КонецПроцедуры
